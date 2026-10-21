#!/usr/bin/env python3
"""OpenAlgo long-box scanner and execution manager (Linux, Python 3.10+).

Normal order APIs only: OpenAlgo chooses Live/Analyze routing.
Never starts, changes mode, or adopts somebody else's positions automatically.
All four legs use one expiry and equal unit quantity.
Default policy is INTRADAY: fixed expiry payoff is an entry valuation reference,
not a guarantee of the price obtainable when closing early.

Keep the journal/lock directory across restarts. Do not delete an active journal.
Use these contracts exclusively in this account while this strategy is running.
"""

from __future__ import annotations
import fcntl
import hashlib
import json
import logging
import math
import os
import signal
import threading
import time
import uuid
from datetime import datetime, time as dt_time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from zoneinfo import ZoneInfo
from openalgo import api

# --------------------------- User configuration ---------------------------
UNDERLYING = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
EXCHANGE = "NFO"
PRODUCT = "NRML"
EXPIRY = ""                       # "" = nearest valid expiry; pinned per box
LOTS = 1                         # Fixed sizing; no martingale
STRIKES_EACH_SIDE = 5
WIDTHS = (100, 200)               # Strike-point widths to scan
CAPITAL_LIMIT_RS = 50_000.0
MIN_ESTIMATED_EDGE_RS = 200.0      # After estimated costs, funding and buffer
MIN_EXIT_PROFIT_RS = 100.0
MAX_CLOSE_LOSS_RS = 1_000.0
MAX_BOXES_PER_DAY = 1
ANNUAL_FUNDING_RATE = 0.10
MARGIN_BUFFER = 1.25
SLIPPAGE_POINTS_PER_LEG = 0.10
# Conservative configurable budgets, NOT an exact statutory tax calculator.
# Calibrate with actual broker/exchange costs before enabling real orders.
FIXED_COST_PER_ORDER_RS = 30.0
PREMIUM_COST_BPS = 30.0           # Budget on entry + estimated exit turnover
SETTLEMENT_COST_RESERVE_RS = 200.0
ENTRY_START = dt_time(9, 20)
ENTRY_END = dt_time(14, 30)
FORCE_EXIT = dt_time(15, 20)
MARKET_OPEN = dt_time(9, 15)
MARKET_END = dt_time(15, 30)
QUOTE_MAX_AGE_SECONDS = 2.0
QUOTE_MAX_SKEW_SECONDS = 1.0
FEED_FAILURE_EXIT_SECONDS = 15.0
SCAN_SECONDS = 2.0
ORDER_POLL_SECONDS = 1.0
ENTRY_ORDER_WINDOW_SECONDS = 5.0
MAX_EXIT_REJECTIONS = 3
HOST = "http://127.0.0.1:5000"
WS_URL = "ws://127.0.0.1:8765"
STRATEGY = "BOX_ARBITRAGE_V1"      # Unique and stable; do not rename mid-position
IST = ZoneInfo("Asia/Kolkata")
BASE = Path(__file__).resolve().parent
# Stable across copies made by the Python strategy scheduler.
STATE_DIR = Path(os.getenv("OPENALGO_BOX_STATE_DIR",
                           "/home/ubuntu/openalgo/strategies/box-arbitrage/state"))
TERMINAL = {"complete", "cancelled", "canceled", "rejected", "expired"}

class RetryLater(Exception):
    """Read request failed; retain state and retry, never infer no fills."""

class NeedsAttention(Exception):
    """Cannot safely infer ownership or outcome. Block new orders."""

def now():
    return datetime.now(IST)

def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite numeric field")
    return result

def integer(value):
    n = number(value)
    if not n.is_integer():
        raise ValueError("Fractional quantity")
    return int(n)

def payload(response):
    if not isinstance(response, dict) or response.get("status") != "success":
        # Do not print full API responses: they may contain sensitive data.
        raise RetryLater("OpenAlgo request did not return success")
    return response.get("data", {})

def rows(response, key):
    data = payload(response)
    if isinstance(data, dict):
        data = data.get(key)
    if not isinstance(data, list):
        raise RetryLater("Unexpected " + key + " response shape")
    return data

def oid(row):
    return str(row.get("orderid") or row.get("order_id") or "")

def order_state(row):
    return str(row.get("order_status", "")).strip().lower()

def mode_from(response):
    data = payload(response)
    if not isinstance(data, dict) or not isinstance(data.get("analyze_mode"), bool):
        raise RetryLater("Cannot establish OpenAlgo routing mode")
    return "analyze" if data["analyze_mode"] else "live"

def expiry_date(value):
    # English month parsing avoids dependence on the process locale.
    months = {m: i + 1 for i, m in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
         "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
    text = value.strip().upper().replace("-", "")
    if len(text) not in (7, 9):
        raise ValueError("Expiry must be DDMMMYY or DDMMMYYYY")
    return datetime(int(text[5:]) + (2000 if len(text) == 7 else 0),
                    months[text[2:5]], int(text[:2]), 15, 30, tzinfo=IST)

def round_price(price, tick, action):
    mode = ROUND_CEILING if action == "BUY" else ROUND_FLOOR
    p, t = Decimal(str(price)), Decimal(str(tick))
    return float((p / t).to_integral_value(rounding=mode) * t)

def load_env():
    # Credentials may already be injected by the Python strategy scheduler.
    for p in (BASE / ".env", Path("/home/ubuntu/openalgo/.env")):
        if p.exists():
            for raw in p.read_text().splitlines():
                text = raw.strip()
                if text and not text.startswith("#") and "=" in text:
                    k, v = text.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))

class BoxStrategy:
    def __init__(self):
        load_env()
        key = os.getenv("OPENALGO_API_KEY")
        if not key:
            raise ValueError("OPENALGO_API_KEY is missing")
        if UNDERLYING not in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}:
            raise ValueError("Only configured index options are supported")
        if (UNDERLYING == "SENSEX") != (EXCHANGE == "BFO"):
            raise ValueError("SENSEX requires BFO; other supported indices require NFO")
        if not isinstance(LOTS, int) or LOTS < 1 or PRODUCT != "NRML":
            raise ValueError("LOTS must be a positive integer and PRODUCT must be NRML")
        if not WIDTHS or any(x <= 0 for x in WIDTHS):
            raise ValueError("WIDTHS must contain positive values")
        for x in (CAPITAL_LIMIT_RS, MIN_ESTIMATED_EDGE_RS, MAX_CLOSE_LOSS_RS,
                  FIXED_COST_PER_ORDER_RS, PREMIUM_COST_BPS, ANNUAL_FUNDING_RATE):
            if x < 0 or not math.isfinite(x):
                raise ValueError("Invalid risk/cost configuration")
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        account = hashlib.sha256((HOST + key).encode()).hexdigest()[:16]
        # Account-wide box lock prevents two copies/variants of this strategy.
        self.lock_file = (STATE_DIR / (account + ".lock")).open("a+")
        os.chmod(self.lock_file.name, 0o600)
        fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.path = STATE_DIR / (account + ".json")
        self.client = api(api_key=key, host=HOST, ws_url=WS_URL, timeout=5.0)
        self.stop = threading.Event()
        self.quote_lock = threading.Lock()
        self.quotes = {}
        self.contracts = {}
        self.subscription = set()
        self.last_universe = 0.0
        self.last_log = {}
        self.log = logging.getLogger(STRATEGY)
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        if not self.log.handlers:
            fmt = logging.Formatter("%(asctime)s IST %(levelname)s %(message)s")
            logging.Formatter.converter = lambda *_: now().timetuple()
            for h in (logging.StreamHandler(),
                      logging.FileHandler(STATE_DIR / (account + ".log"))):
                h.setFormatter(fmt)
                self.log.addHandler(h)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "version": 1, "phase": "IDLE", "box": None, "pending": None,
            "day": now().date().isoformat(), "entries": 0, "halt": None}
        if self.state.get("version") != 1:
            raise NeedsAttention("Unknown journal version; manual migration required")
        self.mode = mode_from(self.client.analyzerstatus())
        saved_box = self.state.get("box")
        if saved_box:
            context = {"underlying": UNDERLYING, "exchange": EXCHANGE,
                       "product": PRODUCT, "strategy": STRATEGY, "host": HOST}
            if saved_box.get("context") != context:
                raise NeedsAttention("Active journal belongs to a different configuration")
            if saved_box["mode"] != self.mode:
                raise NeedsAttention("Mode changed with an active journal; reconcile manually")
            self.contracts.update({leg["symbol"]: leg for leg in saved_box["legs"]})
            self.state["phase"] = "EXITING"  # restart exits known inventory, never resumes entry
            self.save()
        self.log.info("START mode=%s lots=%d exit_policy=INTRADAY journal=%s",
                      self.mode, LOTS, self.path)

    def save(self):
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path)
        directory = os.open(STATE_DIR, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def report(self, label, message, *args, interval=15):
        if time.monotonic() - self.last_log.get(label, -1e9) >= interval:
            self.log.warning(message, *args)
            self.last_log[label] = time.monotonic()

    def halt(self, reason):
        self.state["halt"] = reason
        self.save()
        self.log.error("ATTENTION %s; journal retained; no new orders", reason)

    def assert_mode(self):
        if mode_from(self.client.analyzerstatus()) != self.mode:
            raise NeedsAttention("OpenAlgo mode changed during this process")

    def position_quantities(self):
        result = {}
        for row in rows(self.client.positionbook(), "positions"):
            if row.get("exchange") == EXCHANGE and row.get("product") == PRODUCT:
                symbol = str(row.get("symbol", ""))
                result[symbol] = result.get(symbol, 0) + integer(row.get("quantity", 0))
        return result

    def assert_owned(self):
        box = self.state.get("box")
        if not box:
            return
        actual = self.position_quantities()
        expected = box["inventory"]
        for leg in box["legs"]:
            symbol = leg["symbol"]
            if actual.get(symbol, 0) != expected[symbol]:
                raise NeedsAttention("Position mismatch for " + symbol +
                                     "; manual/shared-contract activity or delayed book")

    def on_depth(self, message):
        try:
            if not isinstance(message, dict) or message.get("exchange") != EXCHANGE:
                return
            symbol = message.get("symbol")
            data = message.get("data", {})
            # SDK adapters may emit lists or numbered buyBook/sellBook maps.
            depth = data.get("depth", {})
            bid = data.get("bids")
            ask = data.get("asks")
            if isinstance(depth, dict):
                if bid is None:
                    bid = depth.get("buy")
                if ask is None:
                    ask = depth.get("sell")
            if bid is None:
                bid = [data.get("buyBook", {}).get("1", {})]
            if ask is None:
                ask = [data.get("sellBook", {}).get("1", {})]
            b, a = bid[0], ask[0]
            bp, ap = number(b["price"]), number(a["price"])
            bq = integer(b.get("quantity", b.get("qty", 0)))
            aq = integer(a.get("quantity", a.get("qty", 0)))
            if not (0 < bp <= ap and bq > 0 and aq > 0):
                return
            ts = data.get("timestamp")
            source_time = number(ts) if ts is not None else None
            if source_time and source_time > 1e11:
                source_time /= 1000
            q = {"bid": bp, "ask": ap, "bid_qty": bq, "ask_qty": aq,
                 "received": time.monotonic(), "timestamp": source_time}
            with self.quote_lock:
                self.quotes[symbol] = q
        except (ValueError, TypeError, KeyError, IndexError):
            return

    def subscribe(self):
        needed = set(self.contracts)
        if needed == self.subscription:
            return
        if self.subscription:
            self.client.unsubscribe_depth(
                [{"symbol": s, "exchange": EXCHANGE} for s in self.subscription])
        with self.quote_lock:
            self.quotes.clear()
        if not self.client.subscribe_depth(
                instruments=[{"symbol": s, "exchange": EXCHANGE} for s in needed],
                on_data_received=self.on_depth):
            raise RetryLater("WebSocket depth subscription failed")
        self.subscription = needed

    def fresh(self, symbols):
        with self.quote_lock:
            snapshots = [dict(self.quotes[s]) for s in symbols if s in self.quotes]
        if len(snapshots) != len(symbols):
            raise RetryLater("Waiting for all contract depth updates")
        receipt_times = [q["received"] for q in snapshots]
        if (time.monotonic() - min(receipt_times) > QUOTE_MAX_AGE_SECONDS
                or max(receipt_times) - min(receipt_times) > QUOTE_MAX_SKEW_SECONDS):
            raise RetryLater("Depth updates are stale or unsynchronized")
        for q in snapshots:
            # Adapter timestamps may be server receipt time, not exchange time.
            if q["timestamp"] is not None:
                age = time.time() - q["timestamp"]
                if age > QUOTE_MAX_AGE_SECONDS or age < -1:
                    raise RetryLater("Depth timestamp outside freshness window")
        return snapshots

    def refresh_universe(self):
        if self.state["box"] or time.monotonic() - self.last_universe < 60:
            return
        expiry = EXPIRY.upper().strip()
        if not expiry:
            dates = payload(self.client.expiry(
                symbol=UNDERLYING, exchange=EXCHANGE, instrumenttype="options"))
            if not isinstance(dates, list):
                raise RetryLater("Unexpected expiry list")
            dates = sorted([d for d in dates if expiry_date(d) > now()], key=expiry_date)
            if not dates:
                raise RetryLater("No unexpired option contracts")
            expiry = dates[0]
        if expiry_date(expiry) <= now():
            raise ValueError("Configured expiry has passed")
        response = self.client.optionchain(
            underlying=UNDERLYING, exchange=INDEX_EXCHANGE,
            expiry_date=expiry, strike_count=STRIKES_EACH_SIDE)
        payload(response)  # validate success
        chain = response.get("chain", [])
        if not isinstance(chain, list) or not chain:
            raise RetryLater("Empty option chain")
        contracts = {}
        strikes = {}
        for row in chain:
            strike = number(row["strike"])
            pair = {}
            for kind in ("ce", "pe"):
                data = row.get(kind)
                if not data:
                    continue
                symbol = str(data["symbol"])
                if not (symbol.startswith(UNDERLYING + expiry)
                        and symbol.endswith(kind.upper())):
                    raise RetryLater("Contract symbol does not match requested expiry/type")
                lot_size = integer(data["lotsize"])
                tick = number(data["tick_size"])
                if lot_size <= 0 or tick <= 0:
                    raise RetryLater("Missing valid contract lot/tick size")
                contract = {"symbol": symbol, "strike": strike, "type": kind.upper(),
                            "lot_size": lot_size, "tick": tick, "expiry": expiry}
                contracts[symbol] = contract
                pair[kind] = contract
            if len(pair) == 2:
                strikes[strike] = pair
        self.contracts = contracts
        self.strikes = strikes
        self.expiry = expiry
        self.subscribe()
        self.last_universe = time.monotonic()
        self.log.info("UNIVERSE %s %s contracts=%d", UNDERLYING, expiry, len(contracts))

    def cost_budget(self, entry_turnover, exit_turnover):
        return (8 * FIXED_COST_PER_ORDER_RS
                + (entry_turnover + exit_turnover) * PREMIUM_COST_BPS / 10_000
                + SETTLEMENT_COST_RESERVE_RS)

    def candidates(self):
        found = []
        for low in sorted(self.strikes):
            for width in WIDTHS:
                high = low + width
                if high not in self.strikes:
                    continue
                # Buying long wings first limits uncovered option-selling risk.
                legs = [dict(self.strikes[low]["ce"], action="BUY"),
                        dict(self.strikes[high]["pe"], action="BUY"),
                        dict(self.strikes[high]["ce"], action="SELL"),
                        dict(self.strikes[low]["pe"], action="SELL")]
                if len({x["lot_size"] for x in legs}) != 1:
                    continue
                qty = LOTS * legs[0]["lot_size"]
                try:
                    quotes = self.fresh([x["symbol"] for x in legs])
                except RetryLater:
                    continue
                if any(q["ask_qty" if leg["action"] == "BUY" else "bid_qty"] < qty
                       for leg, q in zip(legs, quotes)):
                    continue
                for leg, q in zip(legs, quotes):
                    raw = q["ask"] if leg["action"] == "BUY" else q["bid"]
                    leg["limit"] = round_price(raw, leg["tick"], leg["action"])
                debit = sum((1 if l["action"] == "BUY" else -1) * l["limit"] for l in legs)
                if debit <= 0:
                    # Unusual quotes: do not automatically exploit anomalous data.
                    continue
                entry_turnover = qty * sum(l["limit"] for l in legs)
                exit_turnover = qty * sum(q["bid"] if l["action"] == "BUY" else q["ask"]
                                          for l, q in zip(legs, quotes))
                costs = self.cost_budget(entry_turnover, exit_turnover)
                days = max((expiry_date(self.expiry) - now()).total_seconds(), 0) / 86400
                funding = CAPITAL_LIMIT_RS * ANNUAL_FUNDING_RATE * days / 365
                buffer = qty * 4 * SLIPPAGE_POINTS_PER_LEG
                edge = (width - debit) * qty - costs - funding - buffer
                if edge >= MIN_ESTIMATED_EDGE_RS:
                    found.append({"legs": legs, "qty": qty, "width": width,
                                  "expiry": self.expiry, "debit": debit,
                                  "costs": costs, "funding": funding,
                                  "buffer": buffer, "edge": edge})
        return sorted(found, key=lambda b: b["edge"], reverse=True)

    def capital_check(self, box):
        # Check every intermediate basket, not only the fully hedged box.
        basket, peak = [], 0.0
        buy_premiums = sum(l["limit"] * box["qty"] for l in box["legs"]
                           if l["action"] == "BUY")
        for leg in box["legs"]:
            basket.append({"symbol": leg["symbol"], "exchange": EXCHANGE,
                           "action": leg["action"], "product": PRODUCT,
                           "quantity": box["qty"], "pricetype": "LIMIT",
                           "price": leg["limit"]})
            margin = payload(self.client.margin(positions=basket))
            if not isinstance(margin, dict) or "total_margin_required" not in margin:
                raise RetryLater("Broker basket margin unavailable; entry blocked")
            value = number(margin["total_margin_required"])
            if value < 0:
                raise RetryLater("Invalid basket margin")
            peak = max(peak, value)
        required = peak * MARGIN_BUFFER + buy_premiums + box["costs"]
        funds = payload(self.client.funds())
        cash = number(funds["availablecash"])
        if required > min(cash, CAPITAL_LIMIT_RS):
            raise RetryLater("Insufficient available/configured capital for staged entry")
        box["capital_reserved"] = required

    def start_box(self, box):
        self.assert_mode()
        orders = rows(self.client.orderbook(), "orders")
        actual = self.position_quantities()
        symbols = {l["symbol"] for l in box["legs"]}
        if any(actual.get(s, 0) for s in symbols):
            raise RetryLater("Candidate contracts already have account positions")
        if any(r.get("symbol") in symbols and r.get("exchange") == EXCHANGE
               and order_state(r) not in TERMINAL for r in orders):
            raise RetryLater("Candidate contracts have outstanding account orders")
        self.capital_check(box)
        # Margin API calls take time; re-check prices immediately before reserving.
        quotes = self.fresh([l["symbol"] for l in box["legs"]])
        for leg, q in zip(box["legs"], quotes):
            side = "ask" if leg["action"] == "BUY" else "bid"
            if q[side + "_qty"] < box["qty"]:
                raise RetryLater("Depth quantity changed during preflight")
            if ((leg["action"] == "BUY" and q[side] > leg["limit"])
                    or (leg["action"] == "SELL" and q[side] < leg["limit"])):
                raise RetryLater("Prices changed during preflight")
        box.update(context={"underlying": UNDERLYING, "exchange": EXCHANGE,
                            "product": PRODUCT, "strategy": STRATEGY, "host": HOST},
                   mode=self.mode, uid=uuid.uuid4().hex, opened_at=now().isoformat(),
                   funding_rate=ANNUAL_FUNDING_RATE,
                   fixed_order_cost=FIXED_COST_PER_ORDER_RS,
                   premium_cost_bps=PREMIUM_COST_BPS,
                   inventory={s: 0 for s in symbols}, cashflow=0.0,
                   turnover=0.0, completed_orders=0, entry_index=0,
                   exit_rejections=0, entry_date=now().date().isoformat())
        self.state.update(box=box, phase="ENTERING", halt=None)
        self.state["entries"] += 1
        self.save()
        self.log.info("SIGNAL box=%s expiry=%s width=%.0f qty=%d est_edge=%.2f",
                      box["uid"], box["expiry"], box["width"], box["qty"], box["edge"])

    def submit(self, leg, action, qty, kind, price=0.0):
        if self.state["pending"]:
            raise NeedsAttention("Attempted duplicate submission while pending")
        self.assert_mode()
        self.assert_owned()
        order_rows = rows(self.client.orderbook(), "orders")
        if any(r.get("symbol") == leg["symbol"] and r.get("exchange") == EXCHANGE
               and r.get("product") == PRODUCT and order_state(r) not in TERMINAL
               for r in order_rows):
            raise NeedsAttention("Another working order exists for " + leg["symbol"])
        baseline = [oid(r) for r in order_rows]
        box = self.state["box"]
        pending = {"symbol": leg["symbol"], "action": action, "qty": qty,
                   "kind": kind, "price": price, "order_id": None,
                   "baseline": baseline, "submitted": time.time(),
                   "filled": 0, "fill_value": 0.0, "cancel_requested": False,
                   "cancel_at": 0.0, "intent": box["uid"] + "-" + uuid.uuid4().hex[:8]}
        # Write intent BEFORE network request. Timeout or crash never triggers blind retry.
        self.state["pending"] = pending
        self.save()
        try:
            result = self.client.placeorder(
                strategy=STRATEGY, symbol=leg["symbol"], exchange=EXCHANGE,
                action=action, product=PRODUCT, quantity=qty,
                price_type="LIMIT" if kind == "ENTRY" else "MARKET",
                **({"price": price} if kind == "ENTRY" else {}))
            payload(result)
            data = result.get("data", {})
            pending["order_id"] = oid(result) or (oid(data) if isinstance(data, dict) else "")
            if not pending["order_id"]:
                raise RetryLater("Submission returned no order ID")
            self.save()
            self.log.info("ORDER %s %s qty=%d id=%s kind=%s",
                          action, leg["symbol"], qty, pending["order_id"], kind)
        except Exception as exc:
            self.report("submit_unknown", "SUBMISSION UNKNOWN %s; reconcile before any new order",
                        type(exc).__name__, interval=0)
            # Keep the intent; even an error response can follow an accepted request.
            self.save()

    def reconcile_unknown(self, p):
        possible = []
        for row in rows(self.client.orderbook(), "orders"):
            if oid(row) in p["baseline"] or not oid(row):
                continue
            if not (row.get("symbol") == p["symbol"]
                    and row.get("exchange") == EXCHANGE
                    and row.get("product") == PRODUCT
                    and str(row.get("action", "")).upper() == p["action"]
                    and integer(row.get("quantity", 0)) == p["qty"]):
                continue
            if row.get("strategy") and row["strategy"] != STRATEGY:
                continue
            # Brokers do not consistently echo strategy/client IDs. Exclusivity
            # is required; one metadata match is evidence, not server idempotency.
            possible.append(row)
        if len(possible) == 1:
            p["order_id"] = oid(possible[0])
            self.state["phase"] = "EXITING"  # any uncertain entry invalidates this box
            self.save()
            self.log.warning("RECOVERED order_id=%s; cancel/reconcile then unwind", p["order_id"])
        else:
            self.report("unknown", "UNKNOWN outcome for %s; matches=%d; no resubmission. "
                        "Check orderbook and journal manually.", p["symbol"], len(possible))

    def fill_snapshot(self, p):
        data = payload(self.client.orderstatus(order_id=p["order_id"], strategy=STRATEGY))
        if not isinstance(data, dict):
            raise RetryLater("Invalid order status")
        if data.get("symbol") and data["symbol"] != p["symbol"]:
            raise NeedsAttention("Order ID maps to another contract")
        status = order_state(data)
        trade_rows = rows(self.client.tradebook(), "trades")
        qty, value, seen = 0, 0.0, set()
        for row in trade_rows:
            if oid(row) != p["order_id"]:
                continue
            if (row.get("symbol") != p["symbol"] or row.get("exchange") != EXCHANGE
                    or str(row.get("action", "")).upper() != p["action"]):
                raise NeedsAttention("Tradebook order metadata mismatch")
            trade_id = str(row.get("tradeid") or row.get("trade_id") or "")
            if trade_id:
                if trade_id in seen:
                    continue
                seen.add(trade_id)
            n = abs(integer(row.get("quantity", row.get("filled_quantity", 0))))
            price = number(row.get("average_price", row.get("price", 0)))
            if n and price <= 0:
                raise RetryLater("Fill price not available yet")
            qty += n
            value += n * price
        # Prefer per-order cumulative status when supplied, with a valid average.
        status_qty = max([0] + [integer(data[k]) for k in
                        ("filled_quantity", "filled_qty", "traded_quantity") if k in data])
        average = number(data.get("average_price") or 0)
        if status_qty > qty:
            if average <= 0:
                raise RetryLater("Waiting for partial-fill average price")
            qty, value = status_qty, status_qty * average
        if qty > p["qty"] or qty < p["filled"]:
            raise NeedsAttention("Fill quantity inconsistent with journal/request")
        if status == "complete" and qty != p["qty"]:
            # Full-status flag alone is insufficient; wait for fill quantity/price.
            raise RetryLater("Waiting for complete order fills in tradebook")
        return qty, value, status

    def poll_pending(self):
        p = self.state["pending"]
        if not p["order_id"]:
            self.reconcile_unknown(p)
            return
        qty, value, status = self.fill_snapshot(p)
        box = self.state["box"]
        change = qty - p["filled"]
        value_change = value - p["fill_value"]
        sign = 1 if p["action"] == "BUY" else -1
        box["inventory"][p["symbol"]] += sign * change
        box["cashflow"] -= sign * value_change
        box["turnover"] += value_change
        p["filled"], p["fill_value"] = qty, value
        self.save()  # inventory and cumulative fill accounting committed together
        if status not in TERMINAL:
            should_cancel = p["kind"] == "ENTRY" and (
                time.time() - p["submitted"] >= ENTRY_ORDER_WINDOW_SECONDS
                or self.state["phase"] == "EXITING")
            if should_cancel and time.time() - p["cancel_at"] >= 3:
                p["cancel_at"] = time.time()
                self.save()
                try:
                    payload(self.client.cancelorder(order_id=p["order_id"], strategy=STRATEGY))
                    p["cancel_requested"] = True
                    self.save()
                except RetryLater:
                    self.report("cancel", "Cancellation unconfirmed; keeping pending state")
            return
        # A terminal flag does not prove the tradebook is already up to date.
        actual = self.position_quantities()
        if actual.get(p["symbol"], 0) != box["inventory"][p["symbol"]]:
            self.report("terminal_lag", "Terminal order book not reconciled; retain pending")
            if time.time() - p.get("terminal_seen", time.time()) > 30:
                raise NeedsAttention("Terminal order fills/positions cannot be reconciled")
            p.setdefault("terminal_seen", time.time())
            self.save()
            return
        box["completed_orders"] += 1
        if p["kind"] == "ENTRY":
            if status == "complete" and qty == p["qty"] and self.state["phase"] == "ENTERING":
                box["entry_index"] += 1
            else:
                self.state["phase"] = "EXITING"
                self.log.warning("ENTRY INCOMPLETE filled=%d/%d status=%s; unwind",
                                 qty, p["qty"], status)
        elif qty < p["qty"]:
            box["exit_rejections"] += 1
            if box["exit_rejections"] >= MAX_EXIT_REJECTIONS:
                self.halt("Repeated incomplete/rejected exits; remaining inventory retained")
        self.state["pending"] = None
        # Positionbook can lag tradebook: allow a grace period, then check ownership.
        box["reconcile_after"] = time.time() + 3
        self.save()

    def entry_step(self):
        box = self.state["box"]
        i = box["entry_index"]
        if i == 4:
            if any(box["inventory"][l["symbol"]] !=
                   (box["qty"] if l["action"] == "BUY" else -box["qty"])
                   for l in box["legs"]):
                raise NeedsAttention("Completed box has unequal legs")
            self.state["phase"] = "HOLDING"
            self.save()
            self.log.info("BOX OPEN qty=%d net_debit=%.2f expiry_payoff=%.2f",
                          box["qty"], -box["cashflow"], box["width"] * box["qty"])
            return
        if self.stop.is_set() or now().time() >= ENTRY_END:
            self.state["phase"] = "EXITING"
            self.save()
            return
        leg = box["legs"][i]
        quotes = self.fresh([l["symbol"] for l in box["legs"]])
        # Revalue unfilled legs at their original limits and filled legs at actuals.
        remaining = sum((1 if l["action"] == "BUY" else -1) * l["limit"] * box["qty"]
                        for l in box["legs"][i:])
        edge = box["width"] * box["qty"] + box["cashflow"] - remaining
        edge -= box["costs"] + box["funding"] + box["buffer"]
        q = quotes[i]
        side = "ask" if leg["action"] == "BUY" else "bid"
        if (edge < MIN_ESTIMATED_EDGE_RS or q[side + "_qty"] < box["qty"]
                or (leg["action"] == "BUY" and q[side] > leg["limit"])
                or (leg["action"] == "SELL" and q[side] < leg["limit"])):
            self.state["phase"] = "EXITING"
            self.save()
            return
        if leg["action"] == "SELL":
            hedge = box["legs"][0 if leg["type"] == "CE" else 1]["symbol"]
            if box["inventory"][hedge] < box["qty"]:
                raise NeedsAttention("Short entry requested without its filled long hedge")
        self.submit(leg, leg["action"], box["qty"], "ENTRY", leg["limit"])

    def close_value(self):
        box = self.state["box"]
        quotes = self.fresh([l["symbol"] for l in box["legs"]])
        closing, turnover = 0.0, 0.0
        for leg, q in zip(box["legs"], quotes):
            qty = box["inventory"][leg["symbol"]]
            price = q["bid"] if qty > 0 else q["ask"]
            if q["bid_qty" if qty > 0 else "ask_qty"] < abs(qty):
                raise RetryLater("Not enough depth to value entire exit")
            closing += qty * price
            turnover += abs(qty) * price
        fees = ((box["completed_orders"] + 4) * box["fixed_order_cost"]
                + (box["turnover"] + turnover) * box["premium_cost_bps"] / 10000)
        elapsed = max((now() - datetime.fromisoformat(box["opened_at"])).total_seconds(), 0)
        funding = box["capital_reserved"] * box["funding_rate"] * elapsed / (365 * 86400)
        buffer = sum(abs(qty) for qty in box["inventory"].values()) * SLIPPAGE_POINTS_PER_LEG
        return box["cashflow"] + closing - fees - funding - buffer

    def exit_step(self):
        box = self.state["box"]
        inventory = box["inventory"]
        # Buy back ALL shorts before selling ANY remaining long hedge.
        for negative in (True, False):
            for leg in box["legs"]:
                qty = inventory[leg["symbol"]]
                if (negative and qty < 0) or (not negative and qty > 0):
                    self.submit(leg, "BUY" if qty < 0 else "SELL", abs(qty), "EXIT")
                    return
        self.assert_owned()
        elapsed = max((now() - datetime.fromisoformat(box["opened_at"])).total_seconds(), 0)
        estimated_cost = (box["completed_orders"] * box["fixed_order_cost"]
                          + box["turnover"] * box["premium_cost_bps"] / 10000
                          + box["capital_reserved"] * box["funding_rate"]
                          * elapsed / (365 * 86400))
        self.log.info("FLAT box=%s gross=%.2f estimated_net=%.2f",
                      box["uid"], box["cashflow"], box["cashflow"] - estimated_cost)
        history = self.path.parent / (self.path.stem + ".completed.jsonl")
        # Archive before clearing. Duplicate archive after a crash is identifiable by uid.
        fd = os.open(history, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(box) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.state.update(box=None, phase="IDLE")
        self.save()

    def step(self):
        self.assert_mode()
        if self.state["halt"]:
            self.report("halt", "HALTED %s", self.state["halt"])
            return
        market = MARKET_OPEN <= now().time() < MARKET_END and now().weekday() < 5
        # Read pending statuses even outside trading hours; never place an exit
        # on a closed market and mistake its rejection for a flat position.
        if self.state["pending"]:
            if not market or self.stop.is_set():
                self.state["phase"] = "EXITING"
            self.poll_pending()
            return
        box = self.state["box"]
        if box:
            if time.time() < box.get("reconcile_after", 0):
                return
            self.assert_owned()
            if not market:
                self.report("closed", "MARKET CLOSED; inventory journal retained for next session")
                return
            if (self.stop.is_set() or now().time() >= FORCE_EXIT
                    or now().date().isoformat() != box["entry_date"]):
                self.state["phase"] = "EXITING"
                self.save()
            if self.state["phase"] == "ENTERING":
                try:
                    self.entry_step()
                except RetryLater:
                    # Cannot validate remaining execution: don't extend exposure.
                    self.state["phase"] = "EXITING"
                    self.save()
                    raise
            elif self.state["phase"] == "HOLDING":
                try:
                    pnl = self.close_value()
                    box["last_depth_ok"] = time.time()
                except RetryLater:
                    missing_since = box.setdefault("last_depth_ok", time.time())
                    if time.time() - missing_since >= FEED_FAILURE_EXIT_SECONDS:
                        self.state["phase"] = "EXITING"
                        self.log.warning("Exit depth unavailable for %.0fs; unwind at market",
                                         FEED_FAILURE_EXIT_SECONDS)
                    self.save()
                    raise
                self.report("pnl", "BOX MARK executable_estimated_net=%.2f", pnl, interval=30)
                if pnl >= MIN_EXIT_PROFIT_RS or pnl <= -MAX_CLOSE_LOSS_RS:
                    self.state["phase"] = "EXITING"
                    self.save()
            elif self.state["phase"] == "EXITING":
                self.exit_step()
            return
        if self.stop.is_set() or not market or not ENTRY_START <= now().time() < ENTRY_END:
            return
        day = now().date().isoformat()
        if self.state["day"] != day:
            self.state.update(day=day, entries=0)
            self.save()
        if self.state["entries"] >= MAX_BOXES_PER_DAY:
            return
        self.refresh_universe()
        for candidate in self.candidates()[:3]:
            try:
                self.start_box(candidate)
                return
            except RetryLater as exc:
                self.report("preflight", "ENTRY BLOCKED: %s", str(exc))
        self.report("scan", "SCANNING %s %s; no executable box passed gates",
                    UNDERLYING, getattr(self, "expiry", "?"), interval=60)

    def run(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.stop.set())
        connected = self.client.connect()
        if not connected:
            raise RetryLater("WebSocket connection failed")
        try:
            if self.state["box"]:
                self.subscribe()
            while True:
                try:
                    self.step()
                except NeedsAttention as exc:
                    self.halt(str(exc))
                except Exception as exc:
                    # An interrupted entry is no longer a validated basket.
                    # Pending/unknown orders must reconcile before any unwind.
                    if (self.state["box"] and not self.state["pending"]
                            and self.state["phase"] == "ENTERING"):
                        self.state["phase"] = "EXITING"
                        self.save()
                        self.log.warning("Interrupted entry; switch to coordinated unwind")
                    self.report("retry", "RETRY %s; preserving current journal", type(exc).__name__)
                if self.stop.is_set() and not self.state["box"] and not self.state["pending"]:
                    break
                if self.stop.is_set() and (self.state["halt"] or not
                                          MARKET_OPEN <= now().time() < MARKET_END):
                    break  # journal permits recovery; never claim shutdown flattened
                time.sleep(ORDER_POLL_SECONDS if self.state["box"] else SCAN_SECONDS)
        finally:
            self.save()
            self.client.disconnect()
            self.log.info("STOP phase=%s inventory_retained=%s",
                          self.state["phase"], bool(self.state["box"]))

if __name__ == "__main__":
    BoxStrategy().run()
