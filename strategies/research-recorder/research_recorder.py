"""Record one-second NIFTY option quote bars from OpenAlgo for offline research.

This process has no order calls. It records the nearest two active expiries,
the index quote, and eight strikes on either side of ATM. The universe refreshes
every five minutes; contracts discovered earlier in the session stay subscribed
until the session ends so their observations remain complete.
"""

from __future__ import annotations

import csv
import gzip
import logging
import math
import os
import queue
import re
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from openalgo import api


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ENV_PATHS = (ROOT.parent / "config-ui" / ".env", ROOT.parents[1] / ".env")
HOST = "http://127.0.0.1:5000"
WS_URL = "ws://127.0.0.1:8765"
UNDERLYING = "NIFTY"
UNDERLYING_EXCHANGE = "NSE_INDEX"
OPTION_EXCHANGE = "NFO"
STRIKE_COUNT = 8
EXPIRY_COUNT = 2
MAX_SYMBOLS_PER_SESSION = 250
REFRESH_SECONDS = 300
STALE_FEED_SECONDS = 30.0
RECOVERY_INITIAL_BACKOFF_SECONDS = 30.0
RECOVERY_MAX_BACKOFF_SECONDS = 60.0
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)
CALENDAR_DB = ROOT.parents[1] / "db" / "openalgo.db"
_TRADING_DAY_CACHE: dict[date, bool] = {}
SESSION_START = dt_time(9, 10)
SESSION_END = dt_time(15, 35)
MIN_DISK_FREE_GB = 2.0
IST = ZoneInfo("Asia/Kolkata")
QUEUE_MAXSIZE = 100_000
SHUTDOWN = threading.Event()

logger = logging.getLogger("openalgo.research_recorder")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(handler)


def load_api_key() -> str:
    for path in ENV_PATHS:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    key = os.getenv("OPENALGO_API_KEY")
    if not key:
        raise RuntimeError("OPENALGO_API_KEY is missing from the VM environment files")
    return key


def expiry_date_from_text(value: str) -> date | None:
    match = re.search(r"(?<!\d)(\d{1,2})[- ]?([A-Za-z]{3})[- ]?(\d{2}|\d{4})(?!\d)", value)
    if not match:
        return None
    day, month, year = match.groups()
    fmt = "%d%b%y" if len(year) == 2 else "%d%b%Y"
    try:
        return datetime.strptime(f"{day}{month}{year}", fmt).date()
    except ValueError:
        return None


def expiry_candidates(payload: Any) -> list[tuple[date, str]]:
    found: dict[date, str] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            parsed = expiry_date_from_text(value)
            if parsed:
                found[parsed] = parsed.strftime("%d%b%y").upper()

    walk(payload)
    today = datetime.now(IST).date()
    return sorted((day, label) for day, label in found.items() if day >= today)


@dataclass
class SecondBar:
    second: int
    symbol: str
    exchange: str
    first: float
    high: float
    low: float
    close: float
    volume: float | None
    day_open: float | None
    day_high: float | None
    day_low: float | None
    prev_close: float | None


class QuoteRecorder:
    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self.stop_event = threading.Event()
        self.dropped = 0
        self.dropped_lock = threading.Lock()
        self.writer = threading.Thread(target=self._writer_loop, name="bar-writer", daemon=True)
        self.files: dict[date, tuple[Any, csv.DictWriter]] = {}
        self.last_report = time.monotonic()
        self.rows_written = 0
        self.quote_health_lock = threading.Lock()
        self.last_received_by_key: dict[tuple[str, str], float] = {}
        self.last_written_second: dict[tuple[str, str], int] = {}
        self.gap_intervals = 0
        self.unobserved_seconds = 0
        self.maximum_gap_seconds = 0
        self.nonmonotonic_bars = 0

    def on_quote(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        received = time.time()
        event = {"received": received, "message": message}
        parsed = self._event_to_bar(event)
        if parsed is None:
            return
        key, _second, _ltp, _data = parsed
        with self.quote_health_lock:
            self.last_received_by_key[(key[1], key[0])] = time.monotonic()
        try:
            self.events.put_nowait(event)
        except queue.Full:
            with self.dropped_lock:
                self.dropped += 1

    def quote_ages(self, keys: set[tuple[str, str]], now: float,
                   watch_started: dict[tuple[str, str], float]) -> dict[tuple[str, str], float]:
        # Receipt freshness is evidence of observed feed callbacks, not proof
        # that a new exchange trade occurred. Never consult cached SDK snapshots.
        with self.quote_health_lock:
            return {
                key: now - self.last_received_by_key.get(key, watch_started.get(key, now))
                for key in keys
            }

    @staticmethod
    def _number(data: dict[str, Any], key: str) -> float | None:
        try:
            number = float(data.get(key))
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    def _open_writer(self, day: date) -> csv.DictWriter:
        if day in self.files:
            return self.files[day][1]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        free_gb = os.statvfs(DATA_DIR).f_bavail * os.statvfs(DATA_DIR).f_frsize / (1024**3)
        if free_gb < MIN_DISK_FREE_GB:
            raise RuntimeError(f"Only {free_gb:.2f} GB free; refusing to write more capture data")
        path = DATA_DIR / f"nifty_1s_{day:%Y%m%d}.csv.gz"
        is_new = not path.exists() or path.stat().st_size == 0
        handle = gzip.open(path, "at", encoding="utf-8", newline="")
        fields = (
            "timestamp_ist", "symbol", "exchange", "open", "high", "low", "close",
            "volume", "day_open", "day_high", "day_low", "prev_close",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        if is_new:
            writer.writeheader()
        self.files[day] = (handle, writer)
        return writer

    def _write_bar(self, bar: SecondBar) -> None:
        stamp = datetime.fromtimestamp(bar.second, tz=IST)
        key = (bar.symbol, bar.exchange)
        previous = self.last_written_second.get(key)
        if previous is not None:
            previous_day = datetime.fromtimestamp(previous, tz=IST).date()
            if previous_day == stamp.date():
                gap = bar.second - previous
                if gap > 1:
                    self.gap_intervals += 1
                    self.unobserved_seconds += gap - 1
                    self.maximum_gap_seconds = max(self.maximum_gap_seconds, gap)
                elif gap <= 0:
                    self.nonmonotonic_bars += 1
        self.last_written_second[key] = max(previous or bar.second, bar.second)
        writer = self._open_writer(stamp.date())
        writer.writerow({
            "timestamp_ist": stamp.isoformat(timespec="seconds"),
            "symbol": bar.symbol,
            "exchange": bar.exchange,
            "open": bar.first,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            # This quote field is cumulative DAY volume, not one-second
            # traded volume. Research must difference it within symbol/session
            # (and flag resets), never sum these cumulative snapshots.
            "volume": bar.volume,
            "day_open": bar.day_open,
            "day_high": bar.day_high,
            "day_low": bar.day_low,
            "prev_close": bar.prev_close,
        })
        self.rows_written += 1

    def _event_to_bar(self, event: dict[str, Any]) -> tuple[tuple[str, str], int, float, dict[str, Any]] | None:
        message = event["message"]
        data = message.get("data") if isinstance(message.get("data"), dict) else message
        symbol = str(message.get("symbol") or data.get("symbol") or "")
        exchange = str(message.get("exchange") or data.get("exchange") or "")
        ltp = self._number(data, "ltp")
        if not symbol or not exchange or ltp is None or ltp <= 0:
            return None
        second = int(event["received"])
        return (symbol, exchange), second, ltp, data

    def _writer_loop(self) -> None:
        bars: dict[tuple[str, str], SecondBar] = {}
        while not self.stop_event.is_set() or not self.events.empty():
            try:
                event = self.events.get(timeout=0.25)
            except queue.Empty:
                event = None
            if event is not None:
                parsed = self._event_to_bar(event)
                if parsed:
                    key, second, ltp, data = parsed
                    current = bars.get(key)
                    if current is None or second > current.second:
                        if current is not None:
                            self._write_bar(current)
                        bars[key] = SecondBar(
                            second, key[0], key[1], ltp, ltp, ltp, ltp,
                            self._number(data, "volume"), self._number(data, "open"),
                            self._number(data, "high"), self._number(data, "low"),
                            self._number(data, "close"),
                        )
                    elif second == current.second:
                        current.high = max(current.high, ltp)
                        current.low = min(current.low, ltp)
                        current.close = ltp
                        current.volume = self._number(data, "volume") or current.volume
                        current.day_open = self._number(data, "open") or current.day_open
                        current.day_high = self._number(data, "high") or current.day_high
                        current.day_low = self._number(data, "low") or current.day_low
                        current.prev_close = self._number(data, "close") or current.prev_close
            now_second = int(time.time())
            for key, bar in list(bars.items()):
                if bar.second < now_second:
                    self._write_bar(bar)
                    del bars[key]
            if time.monotonic() - self.last_report >= 60:
                with self.dropped_lock:
                    dropped = self.dropped
                    self.dropped = 0
                with self.quote_health_lock:
                    receipts = dict(self.last_received_by_key)
                recent = sum(time.monotonic() - value < STALE_FEED_SECONDS for value in receipts.values())
                logger.info(
                    "[CAPTURE STATUS] rows=%d queued=%d dropped=%d recent_symbols=%d/%d "
                    "gap_intervals=%d unobserved_seconds=%d max_gap_seconds=%d nonmonotonic_bars=%d "
                    "timestamps=receipt_seconds volume=cumulative_day",
                    self.rows_written, self.events.qsize(), dropped, recent, len(receipts),
                    self.gap_intervals, self.unobserved_seconds, self.maximum_gap_seconds,
                    self.nonmonotonic_bars,
                )
                self.last_report = time.monotonic()
        for bar in bars.values():
            self._write_bar(bar)
        for handle, _writer in self.files.values():
            handle.close()
        self.files.clear()

    def start(self) -> None:
        self.writer.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.writer.join(timeout=15)


def is_trading_day(day: date) -> bool:
    if day in _TRADING_DAY_CACHE:
        return _TRADING_DAY_CACHE[day]
    regular_weekday = day.weekday() < 5
    try:
        with sqlite3.connect(f"file:{CALENDAR_DB}?mode=ro", uri=True, timeout=2) as connection:
            rows = connection.execute(
                "SELECT e.is_open FROM market_holidays h "
                "JOIN market_holiday_exchanges e ON e.holiday_id=h.id "
                "WHERE h.holiday_date=? AND e.exchange_code IN ('NSE','NFO')",
                (day.isoformat(),),
            ).fetchall()
        trading_day = all(bool(row[0]) for row in rows) if rows else regular_weekday
    except (sqlite3.Error, OSError) as exc:
        # Failing closed avoids repeatedly reconnecting on an unknown holiday.
        logger.warning("[CALENDAR UNAVAILABLE] %s: %s; capture will wait", day, exc)
        return False
    _TRADING_DAY_CACHE[day] = trading_day
    return trading_day


def current_session() -> bool:
    now = datetime.now(IST)
    return is_trading_day(now.date()) and SESSION_START <= now.time() < SESSION_END


def market_is_open() -> bool:
    now = datetime.now(IST)
    return is_trading_day(now.date()) and MARKET_OPEN <= now.time() < MARKET_CLOSE


def run_session() -> None:
    api_key = load_api_key()
    client = api(api_key=api_key, host=HOST, ws_url=WS_URL)
    recorder = QuoteRecorder()
    subscribed: set[tuple[str, str]] = set()
    last_refresh = 0.0
    refresh_failures = 0
    watch_keys: set[tuple[str, str]] = set()
    watch_started: dict[tuple[str, str], float] = {}
    next_recovery_at = 0.0
    recovery_backoff = RECOVERY_INITIAL_BACKOFF_SECONDS

    def resubscribe_retained() -> bool:
        if not subscribed:
            return True
        instruments = [{"exchange": ex, "symbol": sym} for ex, sym in sorted(subscribed)]
        sent = client.subscribe_quote(instruments, on_data_received=recorder.on_quote)
        # The SDK bool reports frame transmission; fresh callbacks are the
        # capture health check. It is not a per-symbol broker acknowledgement.
        logger.info("[REASSERT UNIVERSE] contracts=%d sdk_frame_sent=%s", len(instruments), sent)
        return bool(sent)

    def refresh_universe() -> None:
        nonlocal last_refresh, refresh_failures, watch_keys
        expiries_response = client.expiry(symbol=UNDERLYING, exchange=OPTION_EXCHANGE, instrumenttype="options")
        if not isinstance(expiries_response, dict) or expiries_response.get("status") != "success":
            raise RuntimeError(f"Expiry lookup failed: {expiries_response}")
        expiries = expiry_candidates(expiries_response)
        if not expiries:
            raise RuntimeError("No current or future NIFTY option expiries returned by OpenAlgo")
        selected_expiries = expiries[:EXPIRY_COUNT]
        current: list[tuple[str, str]] = [(UNDERLYING_EXCHANGE, UNDERLYING)]
        next_watch_keys = {(UNDERLYING_EXCHANGE, UNDERLYING)}
        for expiry, label in selected_expiries:
            chain_response = client.optionchain(
                underlying=UNDERLYING,
                exchange=UNDERLYING_EXCHANGE,
                expiry_date=label,
                strike_count=STRIKE_COUNT,
            )
            if not isinstance(chain_response, dict) or chain_response.get("status") != "success":
                logger.warning("[CHAIN SKIP] expiry=%s response=%s", label, str(chain_response)[:240])
                continue
            chain_rows = [row for row in chain_response.get("chain", []) if isinstance(row, dict)]
            # The nearest expiry's ATM CE/PE should be liquid. Watching these
            # plus spot detects an old-universe outage even if a newly added,
            # unrelated contract still emits quotes.
            atm = chain_response.get("atm_strike")
            if expiry == selected_expiries[0][0] and atm is not None:
                for row in chain_rows:
                    try:
                        is_atm = float(row.get("strike")) == float(atm)
                    except (TypeError, ValueError):
                        is_atm = False
                    if is_atm:
                        for side in ("ce", "pe"):
                            leg = row.get(side)
                            if isinstance(leg, dict) and leg.get("symbol"):
                                next_watch_keys.add((OPTION_EXCHANGE, str(leg["symbol"])))
            for strike in chain_rows:
                for option_type in ("ce", "pe"):
                    leg = strike.get(option_type) if isinstance(strike, dict) else None
                    symbol = leg.get("symbol") if isinstance(leg, dict) else None
                    if symbol:
                        current.append((OPTION_EXCHANGE, str(symbol)))
        new_items = [item for item in current if item not in subscribed]
        room = max(0, MAX_SYMBOLS_PER_SESSION - len(subscribed))
        if len(new_items) > room:
            logger.warning("[UNIVERSE CAP] adding %d of %d new symbols; session cap=%d", room, len(new_items), MAX_SYMBOLS_PER_SESSION)
            new_items = new_items[:room]
        subscribed.update(new_items)
        sent = resubscribe_retained()
        logger.info(
            "[SUBSCRIBE] expiries=%s discovered=%d retained=%d sdk_frame_sent=%s",
            ",".join(label for _day, label in selected_expiries),
            len(new_items), len(subscribed), sent,
        )
        if not sent:
            logger.warning("[SUBSCRIBE RETRY] universe is retained for recovery; request frame was not sent")
        watch_keys = next_watch_keys & subscribed
        now_mono = time.monotonic()
        for key in watch_keys:
            watch_started.setdefault(key, now_mono)
        last_refresh = now_mono
        refresh_failures = 0

    recorder.start()
    try:
        if not client.connect():
            raise RuntimeError("OpenAlgo WebSocket connection failed")
        logger.info("[CONNECTED] research capture only; order methods are not used")
        while current_session() and not SHUTDOWN.is_set():
            try:
                if not last_refresh or time.monotonic() - last_refresh >= REFRESH_SECONDS:
                    refresh_universe()
            except Exception as exc:
                refresh_failures += 1
                logger.exception("[REFRESH RETRY] attempt=%d: %s", refresh_failures, exc)
                # Pace discovery failures instead of hammering the history/
                # option-chain API every second while broker login is invalid.
                last_refresh = time.monotonic() - REFRESH_SECONDS + RECOVERY_INITIAL_BACKOFF_SECONDS

            monotonic_now = time.monotonic()
            ages = recorder.quote_ages(watch_keys, monotonic_now, watch_started)
            stale = {key: age for key, age in ages.items() if age >= STALE_FEED_SECONDS}
            if market_is_open() and stale and monotonic_now >= next_recovery_at:
                detail = ", ".join(f"{ex}:{sym} age={age:.0f}s" for (ex, sym), age in sorted(stale.items()))
                logger.warning("[FEED RECOVERY] stale received data: %s; reconnecting recorder client only", detail)
                next_recovery_at = monotonic_now + recovery_backoff
                recovery_backoff = min(recovery_backoff * 2, RECOVERY_MAX_BACKOFF_SECONDS)
                try:
                    # Keep recorder, writer, files and desired contracts alive.
                    # Only this capture client's socket is re-authenticated.
                    client.disconnect()
                    if not client.connect():
                        raise RuntimeError("Recorder WebSocket reconnect failed")
                    if not resubscribe_retained():
                        raise RuntimeError("Retained-universe replay frame was not sent")
                    logger.info("[FEED RECOVERY SENT] waiting for actual received callbacks; next retry >=%.0fs", next_recovery_at - monotonic_now)
                except Exception:
                    logger.exception("[FEED RECOVERY RETRY] recorder capture connection is unavailable")
            elif market_is_open() and ages and not stale:
                recovery_backoff = RECOVERY_INITIAL_BACKOFF_SECONDS
            time.sleep(1)
    finally:
        try:
            client.unsubscribe_quote([{"exchange": ex, "symbol": sym} for ex, sym in subscribed])
        except Exception:
            logger.exception("[UNSUBSCRIBE] cleanup failed")
        try:
            client.disconnect()
        except Exception:
            logger.exception("[DISCONNECT] cleanup failed")
        recorder.stop()
        logger.info("[SESSION END] rows=%d symbols=%d", recorder.rows_written, len(subscribed))


def main() -> None:
    logger.info("NIFTY research recorder starting; weekdays %s–%s IST", SESSION_START, SESSION_END)
    while not SHUTDOWN.is_set():
        if not current_session():
            time.sleep(30)
            continue
        try:
            run_session()
        except Exception:
            logger.exception("[SESSION RETRY] capture stopped unexpectedly")
            time.sleep(30)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signum, _frame: SHUTDOWN.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: SHUTDOWN.set())
    main()
