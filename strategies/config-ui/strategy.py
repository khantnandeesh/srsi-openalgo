"""Independent OpenAlgo workers for configurable NIFTY option contracts.

Each entry in workers.json runs its own strategy loop and keeps its own
position, order IDs, entry price, and state.

The open candle is represented by appending the latest option LTP from the
OpenAlgo SDK WebSocket feed to completed candle closes before calculating
StochRSI. This is the provisional-close approach for intrabar updates.
"""

from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from openalgo import api, ta
from strategy_config import read_configuration
from price_alerts import observe_price_alert
from incremental_srsi import IncrementalSRSI
from strategy_caches import cached_history
import max_loss


# ---------------------------------------------------------------------------
# Strategy configuration
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"
# The configuration page edits this file; strategy workers read it on startup.
WORKER_CONFIG_FILE = PROJECT_DIR / "workers.json"
LOG_DIR = PROJECT_DIR / "logs"

OPENALGO_HOST = "http://127.0.0.1:5000"
POLL_SECONDS = 1.0
STATUS_LOG_INTERVAL_SECONDS = 15.0
BUY_FILL_WINDOW_SECONDS = 5.0
WEBSOCKET_RETRY_SECONDS = 5.0
MAX_TICK_AGE_SECONDS = 3.0
POSITION_SYNC_SECONDS = 5.0
# Reconciliation reads must fail fast; the SDK default waits 120 seconds.
READ_TIMEOUT_SECONDS = 3.0
# Marketable limit: buy up to this many points above the signal LTP.
BUY_LIMIT_BUFFER_POINTS = 0.5
# If the price comes back from loss into profit while SRSI K is at/above the exit level, do not sell at
# break-even: forget SRSI for that trade and sell at entry + this many points (the stop-loss still applies).
TARGET_AFTER_K80_IN_LOSS_POINTS = 10.0
PRICE_TICK = 0.05
POSITION_BOOK_CACHE_LOCK = threading.Lock()
POSITION_BOOK_CACHE: dict[str, Any] = {}


def _position_book_snapshot(client):
    # Share one account read across workers; never reuse failed responses.
    with POSITION_BOOK_CACHE_LOCK:
        if time.monotonic() - POSITION_BOOK_CACHE.get("at", -float("inf")) < POSITION_SYNC_SECONDS:
            return POSITION_BOOK_CACHE["rows"]
        response = client.positionbook()
        if not isinstance(response, dict) or response.get("status") != "success":
            raise RuntimeError("Position book unavailable")
        rows = response.get("data")
        if isinstance(rows, dict):
            rows = rows.get("positions")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("Unrecognized position book")
        POSITION_BOOK_CACHE.update(at=time.monotonic(), rows=rows)
        return rows

OPTION_CHAIN_CACHE: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
OPTION_CHAIN_CACHE_LOCK = threading.Lock()

CANDLE_ANCHOR_TIME = dt_time(9, 15)
MARKET_OPEN = CANDLE_ANCHOR_TIME
ENTRY_CUTOFF = dt_time(15, 15)
MARKET_CLOSE = dt_time(15, 30)
IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Environment, logging, and OpenAlgo helpers
# ---------------------------------------------------------------------------

def load_env_file(path: Path = ENV_FILE) -> None:
    """Read simple KEY=VALUE entries without printing any secret values."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


# Load credentials without displaying their values. Standalone/manual launches
# default to paper mode. The OpenAlgo scheduled entry point sets this to true
# to preserve the live-order behavior of the currently scheduled strategy.
load_env_file()
API_KEY = os.getenv("OPENALGO_API_KEY")
LIVE_TRADING = os.getenv("OPENALGO_LIVE_TRADING", "false").strip().lower() in {
    "1", "true", "yes", "on",
}


def make_client(timeout: float | None = None):
    if not API_KEY:
        raise RuntimeError("OPENALGO_API_KEY is missing from the environment or .env")
    # The installed OpenAlgo SDK accepts `host` (not `host_url`).
    if timeout is None:
        return api(api_key=API_KEY, host=OPENALGO_HOST)
    return api(api_key=API_KEY, host=OPENALGO_HOST, timeout=timeout)


def _tick_round(price: float) -> float:
    return round(round(price / PRICE_TICK) * PRICE_TICK, 2)


class ISTFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=IST)
        return timestamp.strftime(datefmt) if datefmt else timestamp.isoformat(timespec="seconds")


def make_logger(symbol: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"nifty_{symbol.lower()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        formatter = ISTFormatter(
            "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler = logging.FileHandler(
            LOG_DIR / f"{symbol.lower()}_strategy.log",
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    return logger


def now_ist() -> datetime:
    return datetime.now(IST)


def response_data(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or response.get("status") != "success":
        raise RuntimeError(f"OpenAlgo request failed: {response}")
    data = response.get("data")
    return data if isinstance(data, dict) else {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_ist_index(values: pd.Series) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(pd.to_datetime(values, errors="coerce"))
    if index.tz is None:
        return index.tz_localize(IST)
    return index.tz_convert(IST)


def _normalise_history(response: Any, candle_minutes: int) -> pd.DataFrame:
    if isinstance(response, pd.DataFrame):
        frame = response.copy()
    elif isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, list):
            frame = pd.DataFrame(data)
        elif isinstance(data, dict):
            frame = pd.DataFrame(data)
        else:
            raise RuntimeError(f"Unexpected history response: {response}")
    else:
        raise RuntimeError(f"Unexpected history response type: {type(response)}")

    if frame.empty:
        raise RuntimeError(f"OpenAlgo returned no {candle_minutes}-minute option history")
    if "timestamp" in frame.columns:
        frame.index = _to_ist_index(frame.pop("timestamp"))
    else:
        frame.index = _to_ist_index(pd.Series(frame.index))

    if "close" not in frame.columns:
        raise RuntimeError("History response has no close column")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["close"]).sort_index()
    return frame[~frame.index.duplicated(keep="last")]


def _current_bucket_start(now: datetime, candle_minutes: int) -> datetime:
    # Anchor configured candles to the NSE session open, never process start.
    session_start = datetime.combine(now.date(), CANDLE_ANCHOR_TIME, tzinfo=IST)
    if now <= session_start:
        return session_start
    elapsed_seconds = (now - session_start).total_seconds()
    bucket = int(elapsed_seconds // (candle_minutes * 60))
    return session_start + timedelta(minutes=bucket * candle_minutes)


# ---------------------------------------------------------------------------
# Isolated per-option strategy worker
# ---------------------------------------------------------------------------

@dataclass
class PendingOrder:
    order_id: str
    requested_quantity: int
    submitted_at: float
    kind: str
    price: float = 0.0
    cancel_requested: bool = False
    accounted_fills: int = 0


@dataclass
class LegState:
    phase: str = "WAITING_TO_BUY"
    pending: PendingOrder | None = None
    position_quantity: int = 0
    entry_average_price: float = 0.0
    last_ltp: float | None = None
    candle_bucket_start: datetime | None = None
    completed_closes: list[float] = field(default_factory=list)
    last_k: float = float("nan")
    last_d: float = float("nan")
    last_status_log: float = 0.0
    last_exit_hold_log: float = 0.0
    halted: bool = False


class OptionLegWorker:
    """Own all state and API traffic for one exact option contract."""

    def __init__(self, worker_config: dict[str, Any], settings: dict[str, Any]):
        self.worker_name = str(worker_config["name"])
        self.underlying = str(worker_config["underlying"])
        self.underlying_exchange = str(worker_config["underlying_exchange"])
        self.option_exchange = str(worker_config["option_exchange"])
        self.expiry_date = str(worker_config["expiry_date"])
        self.strike_price = int(worker_config["strike_price"])
        self.option_type = str(worker_config["option_type"])
        self.lots = int(worker_config["lots"])
        self.lot_size = 0
        self.order_quantity = 0
        self.product = str(settings["product"])
        self.candle_minutes = int(worker_config.get("candle_minutes", settings["candle_minutes"]))
        self.stochrsi_rsi_period = int(settings["stochrsi_rsi_period"])
        self.stochrsi_lookback = int(settings["stochrsi_lookback"])
        self.stochrsi_k_smooth = int(settings["stochrsi_k_smooth"])
        self.stochrsi_d_smooth = int(settings["stochrsi_d_smooth"])
        self.entry_k_value = float(settings["entry_k"])
        self.exit_k_level = float(settings["exit_k_level"])
        self.stop_loss_points = float(settings["stop_loss_points"])
        self.entry_premium_min = float(settings["entry_premium_min"])
        self.entry_premium_max = float(settings["entry_premium_max"])
        self.symbol = f"{self.underlying}{self.expiry_date}{self.strike_price}{self.option_type}"
        self.strategy_name = f"{self.underlying}_{self.strike_price}_{self.expiry_date}_{self.option_type}"
        self.client = make_client()  # Separate SDK client for each thread.
        # Fast-failing client for order-status and position reads; orders keep the default.
        self.read_client = make_client(timeout=READ_TIMEOUT_SECONDS)
        self.logger = make_logger(self.symbol)
        self.state = LegState()
        self.stop_event = threading.Event()
        self.runtime_state_lock = threading.RLock()
        self.runtime_pause = threading.Event()
        self.runtime_ready = False
        self.runtime_retired_safely = False
        self.quote_updates: queue.Queue[tuple[datetime, float]] = queue.Queue()
        self.last_ws_tick_monotonic: float | None = None
        self.position_sync_blocked = False
        self.pair_partner = None       # set by the supervisor for paired Manual threads
        self.cooldown = None           # text while a paired thread waits for its partner after a stop-loss
        self.cooldown_since = None     # epoch seconds the cooldown started
        self.last_exit_at = None       # epoch seconds this thread last went flat after a sell
        self._next_position_sync = 0.0
        self._last_position_sync_warning = -float("inf")
        self._last_sync_error = ""
        self._entry_resume_after: datetime | None = None
        self.market_data_client = self.client
        self._srsi_engine = None
        self._srsi_count = 0
        self._srsi_result_cache = None
        self.srsi_cache_hits = 0
        self.srsi_reference_checks = 0
        self.history_cache_status = None

    def _resolve_contract(self) -> None:
        """Resolve an available option-chain leg and its current lot size."""
        cache_key = (self.underlying, self.underlying_exchange, self.expiry_date)
        with OPTION_CHAIN_CACHE_LOCK:
            chain = OPTION_CHAIN_CACHE.get(cache_key)
            if chain is None:
                response = self.client.optionchain(
                    underlying=self.underlying,
                    exchange=self.underlying_exchange,
                    expiry_date=self.expiry_date,
                )
                if not isinstance(response, dict) or response.get("status") != "success":
                    raise RuntimeError(f"Option chain lookup failed for {self.underlying} {self.expiry_date}: {response}")
                chain = response.get("chain")
                if not isinstance(chain, list):
                    raise RuntimeError(f"OpenAlgo returned no option-chain strikes for {self.underlying} {self.expiry_date}")
                OPTION_CHAIN_CACHE[cache_key] = chain

        for item in chain:
            if not isinstance(item, dict):
                continue
            if _as_float(item.get("strike"), default=float("nan")) != float(self.strike_price):
                continue
            leg = item.get(self.option_type.lower())
            if not isinstance(leg, dict):
                break
            symbol = str(leg.get("symbol", "")).strip()
            lot_size = _as_int(leg.get("lotsize"))
            if not symbol or lot_size <= 0:
                raise RuntimeError(f"Option chain returned invalid symbol/lot size for {self.strike_price} {self.option_type}: {leg}")

            self.symbol = symbol
            self.lot_size = lot_size
            self.order_quantity = self.lots * self.lot_size
            self.strategy_name = f"{self.underlying}_{self.strike_price}_{self.expiry_date}_{self.option_type}"
            self.logger = make_logger(self.symbol)
            self.logger.info(
                "[CONTRACT RESOLVED] %s expiry=%s strike=%d type=%s lots=%d lot_size=%d order_quantity=%d",
                self.symbol,
                self.expiry_date,
                self.strike_price,
                self.option_type,
                self.lots,
                self.lot_size,
                self.order_quantity,
            )
            return

        raise RuntimeError(
            f"Strike {self.strike_price} {self.option_type} is not available in the "
            f"{self.underlying} {self.expiry_date} option chain"
        )

    def stop(self) -> None:
        self.stop_event.set()

    def _on_ltp(self, message: Any) -> None:
        """Queue this contract's SDK WebSocket ticks for its worker thread."""
        try:
            if not isinstance(message, dict):
                return
            if message.get("symbol") != self.symbol or message.get("exchange") != self.option_exchange:
                return
            data = message.get("data")
            if not isinstance(data, dict):
                return
            ltp = _as_float(data.get("ltp"), default=float("nan"))
            if not math.isfinite(ltp) or ltp <= 0:
                return

            raw_timestamp = data.get("timestamp")
            tick_time = now_ist()
            timestamp_value = _as_float(raw_timestamp, default=float("nan"))
            if math.isfinite(timestamp_value) and timestamp_value > 0:
                # OpenAlgo's LTP stream provides epoch milliseconds.
                if timestamp_value > 100_000_000_000:
                    timestamp_value /= 1000.0
                tick_time = datetime.fromtimestamp(timestamp_value, tz=IST)
            self.last_ws_tick_monotonic = float(message.get('_received_monotonic') or time.monotonic())
            self.quote_updates.put((tick_time, ltp))
        except Exception:
            self.logger.exception("[WS TICK ERROR] Could not process %s update", self.symbol)

    def _connect_market_data(self) -> bool:
        """Connect and subscribe this worker's isolated option contract."""
        while not self.stop_event.is_set():
            if self._runtime_retire_if_flat():
                return False
            try:
                if self.client.connect():
                    subscribed = self.client.subscribe_ltp(
                        instruments=[{"symbol": self.symbol, "exchange": self.option_exchange}],
                        on_data_received=self._on_ltp,
                    )
                    if subscribed:
                        self.logger.info(
                            "[WS SUBSCRIBED] %s:%s live LTP feed connected",
                            self.option_exchange,
                            self.symbol,
                        )
                        return True
                    self.logger.warning("[WS SUBSCRIBE RETRY] LTP subscription was not accepted for %s", self.symbol)
                else:
                    self.logger.warning("[WS CONNECT RETRY] Could not connect the live LTP feed for %s", self.symbol)
            except Exception:
                self.logger.exception("[WS CONNECT RETRY] Live LTP setup failed for %s", self.symbol)
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.stop_event.wait(WEBSOCKET_RETRY_SECONDS)
        return False

    def _disconnect_market_data(self) -> None:
        self.client.disconnect()

    def _discard_queued_ticks(self) -> None:
        """Drop ticks received while order management paused signal processing."""
        while True:
            try:
                self.quote_updates.get_nowait()
            except queue.Empty:
                return

    def _load_closed_history(self, now: datetime) -> None:
        start_date = (now.date() - timedelta(days=20)).isoformat()
        end_date = now.date().isoformat()
        history, self.history_cache_status = cached_history(
            (self.symbol, self.option_exchange, self.candle_minutes, OPENALGO_HOST),
            _current_bucket_start(now, self.candle_minutes), start_date, end_date,
            lambda first, last: self.client.history(
                symbol=self.symbol, exchange=self.option_exchange,
                interval=f"{self.candle_minutes}m", start_date=first,
                end_date=last, source="api"),
            lambda response: _normalise_history(response, self.candle_minutes),
        )
        current_bucket = _current_bucket_start(now, self.candle_minutes)
        close_times = history.index + pd.Timedelta(minutes=self.candle_minutes)
        completed = history.loc[close_times <= current_bucket, "close"]
        self.state.completed_closes = [float(value) for value in completed.tolist()]
        self.state.candle_bucket_start = current_bucket
        self._srsi_engine = IncrementalSRSI(self.stochrsi_rsi_period, self.stochrsi_lookback,
                                          self.stochrsi_k_smooth, self.stochrsi_d_smooth)
        for close in self.state.completed_closes:
            self._srsi_engine.append(close)
        self._srsi_count = len(self.state.completed_closes)
        self._srsi_result_cache = None
        self.logger.info("[HISTORY CACHE] %s %s", self.symbol, self.history_cache_status)
        self.logger.info(
            "[HISTORY] %s loaded %d completed %dm candles; current candle=%s to %s",
            self.symbol,
            len(self.state.completed_closes),
            self.candle_minutes,
            current_bucket.strftime("%H:%M:%S"),
            (current_bucket + timedelta(minutes=self.candle_minutes)).strftime("%H:%M:%S"),
        )

    def _roll_candle_if_needed(self, now: datetime, previous_ltp: float | None) -> None:
        bucket = _current_bucket_start(now, self.candle_minutes)
        if self.state.candle_bucket_start is None:
            self.state.candle_bucket_start = bucket
            return
        if bucket <= self.state.candle_bucket_start:
            return

        # Close each finished candle with the last tick received before this
        # one (not the current tick, which belongs to the new candle). Candles
        # that passed without ticks close flat at that same price.
        if previous_ltp is not None:
            step = timedelta(minutes=self.candle_minutes)
            skipped = int((bucket - self.state.candle_bucket_start) / step)
            self.state.completed_closes.extend([previous_ltp] * max(1, skipped))
        self.state.candle_bucket_start = bucket

    def _stochrsi_values(self, ltp: float, now: datetime, previous_ltp: float | None = None) -> tuple[float, float]:
        self._roll_candle_if_needed(now, previous_ltp)
        count = len(self.state.completed_closes)
        if self._srsi_engine is None or self._srsi_count > count:
            self._srsi_engine = IncrementalSRSI(self.stochrsi_rsi_period, self.stochrsi_lookback,
                                              self.stochrsi_k_smooth, self.stochrsi_d_smooth)
            self._srsi_count = 0
            self._srsi_result_cache = None
        while self._srsi_count < count:
            self._srsi_engine.append(self.state.completed_closes[self._srsi_count])
            self._srsi_count += 1
        key = (count, float(ltp))
        if self._srsi_result_cache is not None and self._srsi_result_cache[0] == key:
            self.srsi_cache_hits += 1
            return self._srsi_result_cache[1]
        k, d, spread = self._srsi_engine.preview(ltp)
        # Preserve SDK decisions at exact-entry/exit boundaries and nearly flat RSI windows.
        boundary = (math.isfinite(k) and (abs(k-self.entry_k_value) <= 1e-9
                                         or abs(k-self.exit_k_level) <= 1e-9))
        if boundary or (math.isfinite(spread) and spread <= 1e-9):
            self.srsi_reference_checks += 1
            k_values, d_values = ta.stochrsi(
                np.asarray(self.state.completed_closes + [float(ltp)], dtype="float64"),
                rsi_period=self.stochrsi_rsi_period, stoch_period=self.stochrsi_lookback,
                k_period=self.stochrsi_k_smooth, d_period=self.stochrsi_d_smooth,
            )
            k, d = float(k_values[-1]), float(d_values[-1])
        result = (k, d)
        self._srsi_result_cache = (key, result)
        return result

    def _log_status(self, now: datetime, ltp: float, k_value: float, d_value: float) -> None:
        monotonic_now = time.monotonic()
        if monotonic_now - self.state.last_status_log < STATUS_LOG_INTERVAL_SECONDS:
            return
        self.state.last_status_log = monotonic_now

        candle_start = self.state.candle_bucket_start or _current_bucket_start(now, self.candle_minutes)
        candle_end = candle_start + timedelta(minutes=self.candle_minutes)
        stop_level = (
            self.state.entry_average_price - self.stop_loss_points
            if self.state.position_quantity > 0
            else float("nan")
        )
        k_text = f"{k_value:.2f}" if math.isfinite(k_value) else "warmup"
        d_text = f"{d_value:.2f}" if math.isfinite(d_value) else "warmup"
        stop_text = f"{stop_level:.2f}" if math.isfinite(stop_level) else "n/a"
        self.logger.info(
            "[STATUS] %s phase=%s live=%s ltp=%.2f candle=%s-%s K=%s D=%s qty=%d avg_entry=%.2f stop_below=%s entry_band=[%.2f, %.2f]",
            self.symbol,
            self.state.phase,
            LIVE_TRADING,
            ltp,
            candle_start.strftime("%H:%M"),
            candle_end.strftime("%H:%M"),
            k_text,
            d_text,
            self.state.position_quantity,
            self.state.entry_average_price,
            stop_text,
            self.entry_premium_min,
            self.entry_premium_max,
        )

    def _order_status(self, order_id: str) -> tuple[dict[str, Any], str]:
        response = self.read_client.orderstatus(order_id=order_id, strategy=self.strategy_name)
        data = response_data(response)
        return data, str(data.get("order_status", "unknown")).strip().lower()

    def _tradebook_fills(self, order_id: str, action: str) -> tuple[int, float]:
        """Aggregate fills for this worker's one order ID only."""
        response = self.read_client.tradebook()
        if not isinstance(response, dict) or response.get("status") != "success":
            return 0, 0.0
        data = response.get("data")
        if isinstance(data, dict):
            rows = data.get("trades", data.get("data", []))
        else:
            rows = data
        if not isinstance(rows, list):
            return 0, 0.0

        quantity = 0
        value = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("orderid", row.get("order_id", ""))) != str(order_id):
                continue
            if str(row.get("symbol", "")) != self.symbol:
                continue
            if str(row.get("exchange", self.option_exchange)) != self.option_exchange:
                continue
            if str(row.get("action", "")).upper() != action.upper():
                continue
            row_qty = abs(_as_int(row.get("quantity", row.get("filled_quantity", 0))))
            row_price = _as_float(row.get("average_price", row.get("price", 0.0)))
            quantity += row_qty
            value += row_qty * row_price
        average = value / quantity if quantity else 0.0
        return quantity, average

    def _fill_snapshot(self, pending: PendingOrder) -> tuple[int, float, str]:
        data, status = self._order_status(pending.order_id)
        traded_quantity = 0
        for key in ("filled_quantity", "filled_qty", "filledqty", "traded_quantity"):
            if key in data:
                traded_quantity = max(traded_quantity, abs(_as_int(data.get(key))))
        average_price = _as_float(data.get("average_price"))
        book_quantity, book_average = self._tradebook_fills(
            pending.order_id,
            "BUY" if pending.kind == "BUY" else "SELL",
        )
        if book_quantity >= traded_quantity:
            traded_quantity = book_quantity
            if book_average > 0:
                average_price = book_average
        if status == "complete":
            traded_quantity = pending.requested_quantity
            average_price = average_price or pending.price
        return min(traded_quantity, pending.requested_quantity), average_price, status

    def _place_limit_buy(self, ltp: float, now: datetime) -> None:
        if self.runtime_pause.is_set() or max_loss.TRIPPED.is_set() or getattr(self, "cooldown", None):
            return
        if not (self.entry_premium_min <= ltp <= self.entry_premium_max):
            return
        signal_ltp = ltp
        ltp = _tick_round(ltp + BUY_LIMIT_BUFFER_POINTS)
        if not LIVE_TRADING:
            order_id = f"PAPER-{self.option_type}-{time.time_ns()}"
            self.logger.info("[PAPER BUY] %s qty=%d limit=%.2f", self.symbol, self.order_quantity, ltp)
        else:
            # Reserve this leg before the request. If the connection drops
            # after OpenAlgo receives it, do not submit a duplicate buy.
            self.state.phase = "BUY_SUBMIT_UNKNOWN"
            try:
                response = self.client.placeorder(
                    strategy=self.strategy_name,
                    symbol=self.symbol,
                    action="BUY",
                    exchange=self.option_exchange,
                    price_type="LIMIT",
                    product=self.product,
                    quantity=self.order_quantity,
                    price=ltp,
                )
                data = response_data(response)
            except Exception:
                self.state.halted = True
                self.logger.exception("BUY submission outcome unknown for %s; halting this leg to prevent a duplicate", self.symbol)
                raise
            order_id = str(response.get("orderid", data.get("orderid", "")))
            if not order_id:
                self.state.halted = True
                raise RuntimeError(f"BUY response has no order ID: {response}")
            self.logger.info(
                "[BUY LIMIT] %s qty=%d price=%.2f signal_ltp=%.2f order_id=%s fill_window=%.1fs",
                self.symbol,
                self.order_quantity,
                ltp,
                signal_ltp,
                order_id,
                BUY_FILL_WINDOW_SECONDS,
            )

        self.state.pending = PendingOrder(
            order_id=order_id,
            requested_quantity=self.order_quantity,
            submitted_at=time.monotonic(),
            kind="BUY",
            price=ltp,
        )
        self.state.phase = "BUY_PENDING"

    def _cancel_pending(self, pending: PendingOrder) -> bool:
        if not LIVE_TRADING:
            return True
        response = self.client.cancelorder(
            order_id=pending.order_id,
            strategy=self.strategy_name,
        )
        if not isinstance(response, dict) or response.get("status") != "success":
            self.logger.error("[CANCEL] %s order %s: %s", self.symbol, pending.order_id, response)
            return False
        pending.cancel_requested = True
        self.logger.info("[BUY CANCEL REQUESTED] %s order=%s", self.symbol, pending.order_id)
        return True

    def _finish_buy_window(self, pending: PendingOrder) -> None:
        if not LIVE_TRADING:
            self.state.position_quantity = pending.requested_quantity
            self.state.entry_average_price = pending.price
            self.state.pending = None
            self.state.phase = "SELLING"
            self.logger.info("[PAPER FILLED] %s qty=%d avg=%.2f", self.symbol, pending.requested_quantity, pending.price)
            max_loss.on_buy(self.symbol, pending.requested_quantity, pending.price)
            return

        filled, average, status = self._fill_snapshot(pending)
        if status not in {"complete", "cancelled", "canceled", "rejected", "expired"}:
            if not pending.cancel_requested:
                if not self._cancel_pending(pending):
                    return
            # Do not leave a residual buy working while the sell loop is active.
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                time.sleep(0.25)
                filled, average, status = self._fill_snapshot(pending)
                if status in {"complete", "cancelled", "canceled", "rejected", "expired"}:
                    break
            if status not in {"complete", "cancelled", "canceled", "rejected", "expired"}:
                self.logger.error("[BUY CANCEL WAIT] %s order %s is still %s; keeping buy-pending state", self.symbol, pending.order_id, status)
                return

        if filled <= 0:
            self.logger.info(
                "[BUY EXPIRED] %s order %s had no fills after %.1fs",
                self.symbol,
                pending.order_id,
                BUY_FILL_WINDOW_SECONDS,
            )
            self.state.pending = None
            self.state.phase = "WAITING_TO_BUY"
            return

        self.state.position_quantity = filled
        self.state.entry_average_price = average if average > 0 else pending.price
        self.state.pending = None
        self.state.phase = "SELLING"
        max_loss.on_buy(self.symbol, filled, self.state.entry_average_price)
        self.logger.info(
            "[BUY FILLED] %s qty=%d avg=%.2f status=%s",
            self.symbol,
            self.state.position_quantity,
            self.state.entry_average_price,
            status,
        )

    def _start_market_sell(self, reason: str) -> None:
        started = time.monotonic()
        self._sell_reason = reason
        if not self._check_external_position(force=True):
            if self.state.halted or self.state.phase != "SELLING" or self.state.position_quantity <= 0:
                return
            # A slow or failed position read must not block the exit. The smart
            # order targets a flat position, so it never sells more than is held.
            # A working order on this contract still blocks, to avoid a double exit.
            if "working or unknown order" in self._last_sync_error:
                return
            self.logger.warning("[EXIT WITHOUT RECONCILIATION] %s read failed (%s); sending the exit anyway",
                                self.symbol, self._last_sync_error[:120])
            self.position_sync_blocked = False
        quantity = self.state.position_quantity
        if quantity <= 0:
            self.state.phase = "WAITING_TO_BUY"
            return
        if not LIVE_TRADING:
            ltp = self.state.last_ltp or 0.0
            self.logger.info("[PAPER SELL] %s qty=%d at~%.2f reason=%s", self.symbol, quantity, ltp, reason)
            max_loss.on_sell(self.symbol, quantity, ltp, self.state.entry_average_price)
            entry = self.state.entry_average_price
            self.state.position_quantity = 0
            self.state.entry_average_price = 0.0
            self.state.phase = "WAITING_TO_BUY"
            self.last_exit_at = time.time()
            if "STOP_LOSS" in reason:
                self._stop_after_stop_loss(entry, ltp, quantity)
            return

        # Reserve the leg before submitting. If the request times out after
        # reaching OpenAlgo, do not retry blindly and risk an over-sell.
        self.state.phase = "SELL_SUBMIT_UNKNOWN"
        try:
            response = self.client.placesmartorder(
                strategy=self.strategy_name,
                symbol=self.symbol,
                action="SELL",
                exchange=self.option_exchange,
                price_type="MARKET",
                product=self.product,
                quantity=quantity,
                # SmartOrder position_size is the desired final signed
                # position. This strategy exits the long option leg to flat.
                position_size=0,
            )
            data = response_data(response)
        except Exception:
            self.state.halted = True
            self.logger.exception("SELL submission outcome unknown for %s; halting this leg to prevent a duplicate", self.symbol)
            raise
        order_id = str(response.get("orderid", data.get("orderid", "")))
        if not order_id and "already matched" in str(response.get("message", "")).lower():
            # The broker position was already flat (for example closed from the UI).
            self.logger.info("[EXTERNAL EXIT CONFIRMED] %s smart order found no position to sell; back to buy phase", self.symbol)
            self.last_exit_at = time.time()
            max_loss.on_sell(self.symbol, quantity, self.state.last_ltp or 0.0, self.state.entry_average_price)
            self.state.position_quantity = 0
            self.state.entry_average_price = 0.0
            self.state.phase = "WAITING_TO_BUY"
            self._entry_resume_after = now_ist()
            return
        if not order_id:
            self.state.halted = True
            raise RuntimeError(
                f"SELL was not confirmed for {self.symbol}; no order ID was "
                f"returned (mode={response.get('mode')!r}, "
                f"message={response.get('message')!r}). The leg is halted; "
                "check the OpenAlgo position/order book before restarting."
            )
        self.state.pending = PendingOrder(
            order_id=order_id,
            requested_quantity=quantity,
            submitted_at=time.monotonic(),
            kind="SELL",
        )
        self.state.phase = "SELL_PENDING"
        self._sell_signal_ltp = self.state.last_ltp
        self._sell_entry_price = self.state.entry_average_price
        self.logger.info("[SELL MARKET] %s qty=%d id=%s reason=%s signal_to_order_ms=%.0f",
                         self.symbol, quantity, order_id, reason, (time.monotonic() - started) * 1000)

    def _poll_sell_order(self, pending: PendingOrder) -> None:
        filled, average, status = self._fill_snapshot(pending)
        newly_filled = max(0, filled - pending.accounted_fills)
        if newly_filled:
            self.state.position_quantity = max(0, self.state.position_quantity - newly_filled)
            pending.accounted_fills = filled
            max_loss.on_sell(self.symbol, newly_filled, average or self.state.last_ltp or 0.0,
                             getattr(self, "_sell_entry_price", 0.0))

        if status not in {"complete", "cancelled", "canceled", "rejected", "expired"}:
            return

        self.state.pending = None
        if filled > 0 and average > 0:
            entry = getattr(self, "_sell_entry_price", 0.0) or 0.0
            signal = getattr(self, "_sell_signal_ltp", None)
            self.logger.info(
                "[SELL FILLED] %s qty=%d avg=%.2f signal_ltp=%s slippage=%s realized_gross=%.2f fill_ms=%.0f status=%s",
                self.symbol, filled, average,
                f"{signal:.2f}" if signal else "n/a",
                f"{signal - average:.2f}" if signal else "n/a",
                (average - entry) * filled if entry else 0.0,
                (time.monotonic() - pending.submitted_at) * 1000, status,
            )
        if self.state.position_quantity <= 0:
            self.state.position_quantity = 0
            self.state.entry_average_price = 0.0
            self.state.phase = "WAITING_TO_BUY"
            self.last_exit_at = time.time()
            if "STOP_LOSS" in getattr(self, "_sell_reason", ""):
                self._stop_after_stop_loss(getattr(self, "_sell_entry_price", 0.0) or 0.0, average, filled)
                return
            self.logger.info("[FLAT] %s; back to buy phase", self.symbol)
            return

        # A partial/rejected market exit leaves only this leg's remainder to sell.
        self.state.phase = "SELLING"
        self.logger.warning("[SELL REMAINDER] %s remaining=%d status=%s", self.symbol, self.state.position_quantity, status)

    def _confirmed_position_quantity(self) -> int:
        response = self.read_client.openposition(
            strategy=self.strategy_name, symbol=self.symbol,
            exchange=self.option_exchange, product=self.product,
        )
        if not isinstance(response, dict) or response.get("status") != "success":
            raise RuntimeError("Position confirmation unavailable")
        return self._strict_position_quantity(response.get("quantity"))

    @staticmethod
    def _strict_position_quantity(value) -> int:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise RuntimeError("Missing or invalid position quantity") from None
        if not math.isfinite(number) or not number.is_integer():
            raise RuntimeError("Invalid position quantity")
        return int(number)

    def _check_external_position(self, *, force=False) -> bool:
        """Synchronize UI exits without interfering with this worker's pending order."""
        if not LIVE_TRADING or not self.runtime_ready or self.state.pending is not None:
            return True
        if self.state.phase != "SELLING" or self.state.halted:
            return not self.position_sync_blocked
        now = time.monotonic()
        if not force and now < self._next_position_sync:
            return not self.position_sync_blocked
        self._next_position_sync = now + POSITION_SYNC_SECONDS
        try:
            if force:
                quantity = self._confirmed_position_quantity()
            else:
                rows = _position_book_snapshot(self.read_client)
                matched = [row for row in rows if row.get("symbol") == self.symbol
                           and row.get("exchange") == self.option_exchange
                           and row.get("product") == self.product]
                if len(matched) > 1:
                    raise RuntimeError("Multiple position rows for this contract")
                quantity = self._strict_position_quantity(matched[0].get("quantity")) if matched else 0
            previous = self.state.position_quantity
            if quantity == previous and not force and not self.position_sync_blocked:
                return True
            # An outstanding UI order could still fill. Do not reset or sell against it.
            response = self.read_client.orderbook()
            if not isinstance(response, dict) or response.get("status") != "success":
                raise RuntimeError("Order book unavailable during position reconciliation")
            orders = response.get("data")
            if isinstance(orders, dict):
                orders = orders.get("orders")
            if not isinstance(orders, list) or any(not isinstance(row, dict) for row in orders):
                raise RuntimeError("Unrecognized order book during position reconciliation")
            terminal = {"complete", "completed", "filled", "executed", "cancelled", "canceled", "rejected", "expired"}
            for order in orders:
                if (order.get("symbol") == self.symbol and order.get("exchange") == self.option_exchange
                        and order.get("product") == self.product):
                    status = str(order.get("order_status", order.get("status", ""))).strip().lower()
                    if status not in terminal:
                        raise RuntimeError("A working or unknown order remains for this contract")
            # The shared snapshot can predate our buy fill; confirm directly after checking orders.
            quantity = self._confirmed_position_quantity()
            if quantity < 0 or quantity > previous:
                self.state.halted = True
                self.logger.error("[POSITION MISMATCH] %s tracked=%d actual=%d; halted for reconciliation",
                                  self.symbol, previous, quantity)
                self.position_sync_blocked = True
                return False
            self.position_sync_blocked = False
            self._sync_read_failed = False
            if quantity == previous:
                return True
            # Sold outside SRSI (Kotak app, OpenAlgo UI): book the real fill, not the last tick.
            max_loss.on_sell(self.symbol, previous - quantity, self._recent_sell_price() or self.state.last_ltp or 0.0,
                             self.state.entry_average_price)
            self.state.position_quantity = quantity
            if quantity == 0:
                self.last_exit_at = time.time()
                self.state.entry_average_price = 0.0
                self.state.phase = "WAITING_TO_BUY"
                self._entry_resume_after = now_ist()
                self._discard_queued_ticks()
                self.logger.info("[EXTERNAL EXIT CONFIRMED] %s tracked=%d actual=0; %s",
                                 self.symbol, previous,
                                 "stopping as requested" if self.runtime_pause.is_set() else "waiting to buy on a fresh live tick")
            else:
                self.logger.info("[EXTERNAL PARTIAL EXIT] %s tracked=%d actual=%d; managing remaining units",
                                 self.symbol, previous, quantity)
            return False  # Never make a second decision using the pre-reconciliation tick.
        except Exception as exc:
            self.position_sync_blocked = True
            self._sync_read_failed = True
            self._last_sync_error = str(exc).replace("\n", " ")
            if now - self._last_position_sync_warning >= STATUS_LOG_INTERVAL_SECONDS:
                self._last_position_sync_warning = now
                self.logger.warning("[POSITION SYNC WAIT] %s %s; signal actions paused until confirmed",
                                    self.symbol, str(exc).replace("\n", " ")[:160])
            return False

    def _startup_position_guard(self) -> None:
        if not LIVE_TRADING:
            return
        response = self.client.openposition(
            strategy=self.strategy_name,
            symbol=self.symbol,
            exchange=self.option_exchange,
            product=self.product,
        )
        if not isinstance(response, dict) or response.get("status") != "success":
            raise RuntimeError(f"Cannot reconcile startup position for {self.symbol}: {response}")
        quantity = _as_int(response.get("quantity"))
        if quantity < 0:
            raise RuntimeError(f"Unexpected short position for {self.symbol}: {quantity}; worker will not trade")
        if quantity == 0:
            return

        book = self.client.positionbook()
        rows = book.get("data", []) if isinstance(book, dict) and book.get("status") == "success" else []
        if isinstance(rows, dict):
            rows = rows.get("positions", [])
        row = next((r for r in rows if isinstance(r, dict) and r.get("symbol") == self.symbol and r.get("exchange") == self.option_exchange and r.get("product") == self.product), None)
        average = _as_float(row.get("average_price")) if row else 0.0
        if average <= 0:
            raise RuntimeError(f"Found existing {self.symbol} qty={quantity} but no average price; worker will not trade")
        self.state.position_quantity = quantity
        self.state.entry_average_price = average
        self.state.phase = "SELLING"
        self.logger.warning("[RECOVER POSITION] %s qty=%d avg=%.2f", self.symbol, quantity, average)

    def _handle_pending(self) -> None:
        pending = self.state.pending
        if pending is None:
            raise RuntimeError("Pending phase has no pending order")
        if pending.kind == "SELL":
            self._poll_sell_order(pending)
            return
        elapsed = time.monotonic() - pending.submitted_at
        if elapsed >= BUY_FILL_WINDOW_SECONDS:
            self._finish_buy_window(pending)
            return
        if LIVE_TRADING:
            # Start managing the position as soon as the buy completes,
            # instead of waiting out the whole fill window.
            try:
                _data, status = self._order_status(pending.order_id)
            except Exception:
                return
            if status == "complete":
                self._finish_buy_window(pending)
        else:
            self._finish_buy_window(pending)

    def _stop_after_stop_loss(self, entry: float = 0.0, exit_price: float = 0.0, quantity: int = 0) -> None:
        """A thread that hits its stop-loss stops: no new buys, it retires now that it is flat."""
        self.sl_trade = {"symbol": self.symbol, "entry": entry, "exit": exit_price, "qty": quantity}
        if getattr(self, "pair_partner", None):
            # Paired: no delete. Wait until the partner finishes one trade, then trade again.
            self.cooldown = "Cooling down · waiting for the paired thread to finish a trade"
            self.cooldown_since = time.time()
            self.sl_cooldown_at = now_ist().isoformat(timespec="seconds")
            self.logger.warning("[PAIR COOLDOWN] %s hit its stop-loss; no buys until %s finishes a trade",
                                self.symbol, self.pair_partner)
            return
        self.stopped_after_sl = now_ist().isoformat(timespec="seconds")
        self.runtime_pause.set()
        self.logger.warning("[STOPPED AFTER SL] %s hit its stop-loss; this thread is turned off", self.symbol)

    def _recent_sell_price(self) -> float | None:
        """Average of the newest SELL trade on this contract in the broker tradebook, if readable."""
        try:
            response = self.read_client.tradebook()
            rows = response.get("data") if isinstance(response, dict) else None
            rows = [r for r in rows or [] if isinstance(r, dict) and r.get("symbol") == self.symbol
                    and str(r.get("action", "")).upper() == "SELL"]
            if not rows:
                return None
            newest = max(rows, key=lambda r: datetime.strptime(str(r.get("timestamp")), "%d-%b-%Y %H:%M:%S")
                         if r.get("timestamp") else datetime.min)
            price = _as_float(newest.get("average_price"))
            return price if price > 0 else None
        except Exception:
            return None

    def _max_loss_exit(self) -> bool:
        """Max loss today reached: cancel a working buy and sell what is held at market."""
        if not self.runtime_ready or not (MARKET_OPEN <= now_ist().time() < MARKET_CLOSE):
            return False
        self.runtime_pause.set()
        if self.state.phase == "BUY_PENDING" and self.state.pending is not None:
            self._finish_buy_window(self.state.pending)   # cancels the unfilled rest of the buy
        if self.state.phase == "SELLING" and self.state.position_quantity > 0:
            if time.monotonic() - getattr(self, "_max_loss_sell_at", -1e9) < 2:
                return True
            self._max_loss_sell_at = time.monotonic()
            self.logger.warning("[SELL SIGNAL] %s reason=%s ltp=%s qty=%d", self.symbol, max_loss.TRIP_REASON,
                                self.state.last_ltp, self.state.position_quantity)
            self._start_market_sell(max_loss.TRIP_REASON)
            return True
        return False

    def _runtime_check_draining_restart(self) -> None:
        if not self.runtime_pause.is_set() or not LIVE_TRADING:
            return
        response = self.client.orderbook()
        if not isinstance(response, dict) or response.get("status") != "success":
            raise RuntimeError("Cannot reconcile pending orders for stop-after-exit restart")
        rows = response.get("data")
        if isinstance(rows, dict):
            rows = rows.get("orders")
        if not isinstance(rows, list):
            raise RuntimeError("Unrecognized order book; stop-after-exit restart needs reconciliation")
        terminal = {"complete", "completed", "filled", "executed", "cancelled", "canceled", "rejected", "expired"}
        for row in rows:
            if not isinstance(row, dict) or row.get("symbol") != self.symbol:
                continue
            if row.get("exchange", self.option_exchange) != self.option_exchange:
                continue
            if row.get("product", self.product) != self.product:
                continue
            status = str(row.get("order_status", row.get("status", ""))).strip().lower()
            if status not in terminal:
                raise RuntimeError("Working/unknown order exists for this contract; reconcile it before restarting")

    def _runtime_retire_if_flat(self) -> bool:
        if (self.runtime_pause.is_set() and self.runtime_ready
                and not self.state.halted and self.state.phase == "WAITING_TO_BUY"
                and self.state.pending is None and self.state.position_quantity == 0):
            self.runtime_retired_safely = True
            self.stop_event.set()
            self.logger.info("[RUNTIME OFF] Exit complete; this worker is stopping")
            return True
        return False

    def _tick(self, ltp=None, quote_time=None, *, poll_pending=False) -> None:
        with self.runtime_state_lock:
            if self._runtime_retire_if_flat():
                return
            self._strategy_tick(ltp, quote_time, poll_pending=poll_pending)
            self._runtime_retire_if_flat()

    def _strategy_tick(
        self,
        ltp: float | None = None,
        quote_time: datetime | None = None,
        *,
        poll_pending: bool = False,
    ) -> None:
        if self.state.halted:
            return
        if ltp is not None:
            # Close finished candles with the previous tick before recording this one.
            # Before any tick (just after history load), the current tick is the only estimate.
            previous = self.state.last_ltp if self.state.last_ltp is not None else ltp
            self._roll_candle_if_needed(quote_time or now_ist(), previous)
            self.state.last_ltp = ltp
            alert_time = quote_time or now_ist()
            if MARKET_OPEN <= alert_time.time() < MARKET_CLOSE:
                observe_price_alert(self.worker_name, self.symbol, ltp, self.logger)
        exits_only = False
        if not self._check_external_position():
            # Broker read failed (not a mismatch): keep the stop-loss and exits running, but never buy.
            if not (getattr(self, "_sync_read_failed", False) and not self.state.halted
                    and self.state.phase == "SELLING" and self.state.position_quantity > 0):
                return
            exits_only = True
        if max_loss.TRIPPED.is_set() and self._max_loss_exit():
            return
        if self.state.phase in {"BUY_PENDING", "SELL_PENDING"}:
            if poll_pending:
                self._handle_pending()
            return

        if ltp is None:
            return

        now = quote_time or now_ist()
        if now.time() < MARKET_OPEN or now.time() >= MARKET_CLOSE:
            return

        k_value, d_value = self._stochrsi_values(ltp, now)
        self.state.last_ltp = ltp
        self.state.last_k = k_value
        self.state.last_d = d_value
        self._log_status(now, ltp, k_value, d_value)

        if self.state.phase == "WAITING_TO_BUY":
            if exits_only or getattr(self, "cooldown", None):
                return
            if self._entry_resume_after is not None and now <= self._entry_resume_after:
                return
            if now.time() >= ENTRY_CUTOFF:
                return
            if k_value == self.entry_k_value and self.entry_premium_min <= ltp <= self.entry_premium_max:
                self.logger.info("[BUY SIGNAL] %s K=%.4f LTP=%.2f", self.symbol, k_value, ltp)
                self._place_limit_buy(ltp, now)
            return

        if self.state.phase == "SELLING":
            entry = self.state.entry_average_price
            chase = getattr(self, "_chase", None)
            if chase and chase[0] != entry:
                chase = self._chase = None          # a new trade: start from the normal rules
            if getattr(self, "_below_entry_for", None) != entry:
                self._below_entry_for, self._was_below = entry, False
            was_below, self._was_below = self._was_below, ltp < entry
            if ltp < self.state.entry_average_price - self.stop_loss_points:
                self.logger.info(
                    "[SELL SIGNAL] %s reason=STOP_LOSS_BELOW_15_POINTS ltp=%.2f stop=%.2f K=%.2f qty=%d",
                    self.symbol,
                    ltp,
                    self.state.entry_average_price - self.stop_loss_points,
                    k_value,
                    self.state.position_quantity,
                )
                self._start_market_sell("STOP_LOSS_BELOW_15_POINTS")
            elif chase:
                # SRSI no longer matters for this trade: only entry + 10 (or the stop-loss) closes it.
                if ltp >= chase[1]:
                    self.logger.info("[SELL SIGNAL] %s reason=TARGET_PLUS_%g_AFTER_K80_IN_LOSS ltp=%.2f target=%.2f entry=%.2f qty=%d",
                                     self.symbol, TARGET_AFTER_K80_IN_LOSS_POINTS, ltp, chase[1], entry, self.state.position_quantity)
                    self._start_market_sell(f"TARGET_PLUS_{TARGET_AFTER_K80_IN_LOSS_POINTS:g}_AFTER_K80_IN_LOSS")
            elif math.isfinite(k_value) and k_value >= self.exit_k_level and ltp >= entry and was_below:
                # Just crossed from loss into profit with K >= exit level: chase entry + 10 instead of break-even.
                self._chase = (entry, _tick_round(entry + TARGET_AFTER_K80_IN_LOSS_POINTS))
                self.logger.info(
                    "[TARGET MODE] %s crossed from loss to LTP=%.2f >= entry=%.2f with K=%.2f >= %.2f; selling only at "
                    "target %.2f or stop %.2f, SRSI ignored for this trade",
                    self.symbol, ltp, entry, k_value, self.exit_k_level, self._chase[1], entry - self.stop_loss_points)
            elif math.isfinite(k_value) and k_value >= self.exit_k_level:
                if ltp >= self.state.entry_average_price:
                    self.logger.info(
                        "[SELL SIGNAL] %s reason=STOCHRSI_K_GE_%.2f_AT_OR_ABOVE_ENTRY ltp=%.2f K=%.2f D=%.2f qty=%d avg_entry=%.2f",
                        self.symbol,
                        self.exit_k_level,
                        ltp,
                        k_value,
                        d_value,
                        self.state.position_quantity,
                        self.state.entry_average_price,
                    )
                    self._start_market_sell(f"STOCHRSI_K_GE_{self.exit_k_level:g}_AT_OR_ABOVE_ENTRY")
                else:
                    monotonic_now = time.monotonic()
                    if monotonic_now - self.state.last_exit_hold_log >= STATUS_LOG_INTERVAL_SECONDS:
                        self.logger.info("[EXIT HOLD] %s K=%.2f >= %.2f but LTP=%.2f is below entry=%.2f; holding until stop %.2f",
                                         self.symbol, k_value, self.exit_k_level, ltp, entry, entry - self.stop_loss_points)
                        self.state.last_exit_hold_log = monotonic_now

    def run(self) -> None:
        try:
            self._resolve_contract()
            self._load_closed_history(now_ist())
            self._startup_position_guard()
            self._runtime_check_draining_restart()
            self.runtime_ready = True
        except Exception:
            self.state.halted = True
            self.logger.exception("Worker startup failed; this leg is halted")
            return

        if self._runtime_retire_if_flat():
            return

        if not self._connect_market_data():
            self.logger.info("Worker stopped before market-data subscription for %s", self.symbol)
            return

        self.logger.info(
            "[CONFIG] worker=%s %s strategy=%s expiry=%s strike=%d type=%s lots=%d lot_size=%d qty=%d product=%s candle=%dm anchor=%s feed=OpenAlgo-WebSocket-LTP pending_order_poll=%ss SRSI_RSI_len=%d SRSI_len=%d K=%d D=%d entry_K=%.2f exit_K>=%.2f stop=lt(entry-%.2f) premium_band=[%.2f, %.2f] live_orders=%s",
            self.worker_name,
            self.symbol,
            self.strategy_name,
            self.expiry_date,
            self.strike_price,
            self.option_type,
            self.lots,
            self.lot_size,
            self.order_quantity,
            self.product,
            self.candle_minutes,
            CANDLE_ANCHOR_TIME.strftime("%H:%M"),
            POLL_SECONDS,
            self.stochrsi_rsi_period,
            self.stochrsi_lookback,
            self.stochrsi_k_smooth,
            self.stochrsi_d_smooth,
            self.entry_k_value,
            self.exit_k_level,
            self.stop_loss_points,
            self.entry_premium_min,
            self.entry_premium_max,
            LIVE_TRADING,
        )
        self.logger.info("[WORKER STARTED] %s; state and order tracking are isolated to this leg", self.symbol)
        next_order_poll = time.monotonic()
        last_ws_health_log = time.monotonic()
        try:
            while not self.stop_event.is_set():
                if self._runtime_retire_if_flat():
                    break
                monotonic_now = time.monotonic()
                pending_phase = self.state.phase in {"BUY_PENDING", "SELL_PENDING"}
                timeout = (
                    max(0.0, next_order_poll - monotonic_now)
                    if pending_phase
                    else POLL_SECONDS
                )
                try:
                    tick_time, ltp = self.quote_updates.get(timeout=timeout)
                except queue.Empty:
                    tick_time, ltp = None, None

                if ltp is not None:
                    # Ignore delayed ticks so a reconnect/backlog cannot trigger
                    # an order using stale prices.
                    tick_age = (now_ist() - tick_time).total_seconds()
                    if -MAX_TICK_AGE_SECONDS <= tick_age <= MAX_TICK_AGE_SECONDS:
                        try:
                            self._tick(ltp, tick_time)
                        except Exception:
                            # State remains local; the other worker keeps running.
                            self.logger.exception("Worker tick failed for %s", self.symbol)

                # Poll reconciliation even without live ticks or outside market hours.
                if time.monotonic() >= next_order_poll:
                    was_pending = self.state.phase in {"BUY_PENDING", "SELL_PENDING"}
                    try:
                        self._tick(poll_pending=True)
                    except Exception:
                        self.logger.exception("Pending-order poll failed for %s", self.symbol)
                    next_order_poll = time.monotonic() + POLL_SECONDS
                    if was_pending and self.state.phase not in {"BUY_PENDING", "SELL_PENDING"}:
                        self._discard_queued_ticks()

                monotonic_now = time.monotonic()
                if monotonic_now - last_ws_health_log >= STATUS_LOG_INTERVAL_SECONDS:
                    last_ws_health_log = monotonic_now
                    tick_age = (
                        monotonic_now - self.last_ws_tick_monotonic
                        if self.last_ws_tick_monotonic is not None
                        else None
                    )
                    if tick_age is None or tick_age >= STATUS_LOG_INTERVAL_SECONDS:
                        age_text = "no tick received yet" if tick_age is None else f"no tick for {tick_age:.0f}s"
                        self.logger.warning(
                            "[WS NO RECENT TICK] %s %s; connected=%s authenticated=%s phase=%s",
                            self.symbol,
                            age_text,
                            self.market_data_client.connected,
                            self.market_data_client.authenticated,
                            self.state.phase,
                        )
        finally:
            try:
                self._disconnect_market_data()
            except Exception:
                self.logger.exception("WebSocket disconnect failed for %s", self.symbol)
            self.logger.info("Worker stopped for %s", self.symbol)


def main() -> None:
    load_env_file()
    from worker_supervisor import WorkerSupervisor
    from automatic_worker import AutomaticWorker, CaptureAllWorker
    def factory(row, settings):
        if not row.get("automatic"):
            return OptionLegWorker(row, settings)
        # Capture all: one worker per in-band strike; Single keeps the original one-strike worker.
        engine = CaptureAllWorker if row.get("capture") == "all" else AutomaticWorker
        return engine(row, settings, OptionLegWorker)
    WorkerSupervisor(factory).run()


if __name__ == "__main__":
    main()
