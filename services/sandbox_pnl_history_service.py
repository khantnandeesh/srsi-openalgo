"""Persistent Analyze P&L history from recorded fills and observed account marks.

Sandbox fills use naive IST timestamps. New snapshots use naive UTC timestamps.
Old periods without stored MTM retain a realized-only curve; today's quotes are
never used to invent historical unrealized P&L.
"""

from bisect import bisect_right
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from heapq import merge
from math import ceil
from threading import Lock
from zoneinfo import ZoneInfo

from sqlalchemy.orm import sessionmaker

from database.sandbox_db import (
    SandboxDailyPnL,
    SandboxFunds,
    SandboxHoldings,
    SandboxPnLSnapshot,
    SandboxTrades,
    engine,
)
from database.token_db import get_symbol_info
from utils.logging import get_logger

logger = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
SNAPSHOT_INTERVAL_SECONDS = 15
MAX_RANGE_DAYS = 366
MAX_CHART_POINTS = 12000
Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
_snapshot_lock = Lock()
ZERO = Decimal("0")


def _decimal(value):
    try:
        result = Decimal(str(value or 0))
        return result if result.is_finite() else ZERO
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


def _money(value):
    return round(float(value), 2)


def _utc_naive(value):
    if value.tzinfo is None:
        # Optional snapshot "now" follows the new table's UTC convention.
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _ist_trade_time(value):
    return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)


def _snapshot_time(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _milliseconds(value):
    return int(value.timestamp() * 1000)


def _parse_date(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError("Choose start and end dates in YYYY-MM-DD format.") from None


def record_pnl_snapshot(user_id, now=None):
    """Save already committed account MTM, at most once every 15 seconds.

    This function makes no broker requests, never changes account balances, and
    commits through its own session so the caller's transaction is untouched.
    Returns True when an observed snapshot was written.
    """
    stamp = _utc_naive(now or datetime.now(UTC))
    with _snapshot_lock:
        with Session() as session:
            try:
                latest = (
                    session.query(SandboxPnLSnapshot.timestamp)
                    .filter(SandboxPnLSnapshot.user_id == user_id)
                    .order_by(SandboxPnLSnapshot.timestamp.desc())
                    .first()
                )
                if latest and (stamp - latest[0]).total_seconds() < SNAPSHOT_INTERVAL_SECONDS:
                    return False
                funds = session.query(SandboxFunds).filter_by(user_id=user_id).first()
                if funds is None:
                    return False
                holdings = session.query(SandboxHoldings).filter_by(user_id=user_id).all()
                holding_unrealized = sum(
                    (_decimal(row.pnl) for row in holdings if row.quantity), ZERO
                )
                # Holdings represent cash invested outside used_margin.
                holding_value = sum(
                    (
                        _decimal(row.quantity)
                        * (_decimal(row.ltp) if _decimal(row.ltp) > 0 else _decimal(row.average_price))
                        for row in holdings
                        if row.quantity
                    ),
                    ZERO,
                )
                position_unrealized = _decimal(funds.unrealized_pnl)
                realized = _decimal(funds.realized_pnl)
                unrealized = position_unrealized + holding_unrealized
                available = _decimal(funds.available_balance)
                margin = _decimal(funds.used_margin)
                session.add(
                    SandboxPnLSnapshot(
                        user_id=user_id,
                        timestamp=stamp,
                        realized_pnl=realized,
                        unrealized_pnl=unrealized,
                        total_pnl=realized + unrealized,
                        equity=available + margin + position_unrealized + holding_value,
                        available_balance=available,
                        used_margin=margin,
                        total_capital=_decimal(funds.total_capital),
                    )
                )
                session.commit()
                return True
            except Exception:
                session.rollback()
                logger.exception("Could not persist Analyze P&L snapshot")
                return False


def _contract_multiplier(symbol, exchange, cache):
    key = (symbol, exchange)
    if key not in cache:
        try:
            info = get_symbol_info(symbol, exchange)
            cache[key] = _decimal(getattr(info, "contract_value", 1)) or Decimal("1")
        except Exception:
            cache[key] = Decimal("1")
    return cache[key]


def _replay_fills(trades, start, end):
    """Replay weighted average fills, including partial closes and reversals."""
    positions = {}
    multipliers = {}
    realized = ZERO
    baseline = ZERO
    carry_in = 0
    baseline_set = False
    events = []
    all_times = []
    all_values = []
    daily_realized = {}
    daily_trade_counts = {}
    invalid = 0
    selected_trade_count = 0
    for row in trades:
        stamp = _ist_trade_time(row.trade_timestamp)
        if not baseline_set and stamp >= start:
            baseline = realized
            carry_in = sum(1 for quantity, _average in positions.values() if quantity)
            baseline_set = True
        action = str(row.action or "").upper()
        qty = int(row.quantity or 0)
        price = _decimal(row.price)
        if action not in {"BUY", "SELL"} or qty <= 0 or price <= 0:
            invalid += 1
            continue
        key = (row.symbol, row.exchange, row.product)
        old_qty, old_average = positions.get(key, (0, ZERO))
        signed_qty = qty if action == "BUY" else -qty
        delta = ZERO
        if old_qty == 0:
            new_qty, new_average = signed_qty, price
        elif (old_qty > 0) == (signed_qty > 0):
            new_qty = old_qty + signed_qty
            new_average = (
                old_average * abs(old_qty) + price * abs(signed_qty)
            ) / abs(new_qty)
        else:
            closed_qty = min(abs(old_qty), abs(signed_qty))
            direction = Decimal("1") if old_qty > 0 else Decimal("-1")
            delta = (
                (price - old_average)
                * closed_qty
                * direction
                * _contract_multiplier(row.symbol, row.exchange, multipliers)
            )
            new_qty = old_qty + signed_qty
            new_average = ZERO if not new_qty else (
                price if (new_qty > 0) != (old_qty > 0) else old_average
            )
        positions[key] = (new_qty, new_average)
        realized += delta
        millis = _milliseconds(stamp)
        all_times.append(millis)
        all_values.append(realized)
        if start <= stamp < end:
            selected_trade_count += 1
            day = stamp.date().isoformat()
            daily_realized[day] = daily_realized.get(day, ZERO) + delta
            daily_trade_counts[day] = daily_trade_counts.get(day, 0) + 1
            # Opening fills don't change realized P&L, but retain timestamps.
            events.append((millis, realized, ZERO, "executions_realized"))
    if not baseline_set:
        baseline = realized
        carry_in = sum(1 for quantity, _average in positions.values() if quantity)
    return {
        "baseline": baseline,
        "carry_in": carry_in,
        "events": events,
        "times": all_times,
        "values": all_values,
        "end_realized": realized,
        "daily_realized": daily_realized,
        "daily_trade_counts": daily_trade_counts,
        "trade_count": selected_trade_count,
        "invalid": invalid,
    }


def _realized_at(replay, millis):
    index = bisect_right(replay["times"], millis) - 1
    return replay["values"][index] if index >= 0 else ZERO


def _daily_mark(row):
    """Accept only an actually recorded 23:59 IST mark, never catch-up guesses."""
    if row.created_at is None:
        return None
    created = _snapshot_time(row.created_at).astimezone(IST)
    if created.date() != row.date or created.time().replace(tzinfo=None) < time(23, 55):
        return None
    # Use actual observation time, not a backdated synthetic trading close.
    unrealized = _decimal(row.positions_unrealized_pnl) + _decimal(row.holdings_unrealized_pnl)
    return _milliseconds(created), unrealized


def _series_and_metrics(events, baseline, lower_ms, upper_ms):
    """Preserve exact extrema while bounding chart size using bucket extrema."""
    bucket_width = max(1, ceil(max(1, upper_ms - lower_ms) / (MAX_CHART_POINTS // 5)))
    compact = []
    active_bucket = None
    bucket_points = []
    max_point = min_point = last_point = None
    peak = ZERO
    worst_drawdown = ZERO
    count = 0

    def emit_bucket():
        if not bucket_points:
            return
        first, minimum, maximum, deepest, last = bucket_points
        unique = {item["time"]: item for item in (first, minimum, maximum, deepest, last)}
        compact.extend(unique[stamp] for stamp in sorted(unique))

    for stamp, realized, unrealized, source in events:
        value = realized + unrealized - baseline
        peak = max(peak, value)
        drawdown = value - peak
        worst_drawdown = min(worst_drawdown, drawdown)
        point = {
            "time": stamp,
            "value": _money(value),
            "realized": _money(realized),
            "unrealized": _money(unrealized) if source != "executions_realized" else None,
            "source": source,
            "_exact": value,
            "_drawdown": drawdown,
        }
        count += 1
        last_point = point
        if max_point is None or value > max_point["_exact"]:
            max_point = point
        if min_point is None or value < min_point["_exact"]:
            min_point = point
        bucket = (stamp - lower_ms) // bucket_width
        if bucket != active_bucket:
            emit_bucket()
            active_bucket = bucket
            bucket_points = [point, point, point, point, point]
        else:
            bucket_points[4] = point
            if value < bucket_points[1]["_exact"]:
                bucket_points[1] = point
            if value > bucket_points[2]["_exact"]:
                bucket_points[2] = point
            if drawdown < bucket_points[3]["_drawdown"]:
                bucket_points[3] = point
    emit_bucket()

    def label(point):
        return datetime.fromtimestamp(point["time"] / 1000, IST).strftime("%Y-%m-%d %H:%M") if point else None

    pnl_series = [{key: value for key, value in point.items() if not key.startswith("_")} for point in compact]
    drawdown_series = [{"time": point["time"], "value": _money(point["_drawdown"])} for point in compact]
    return {
        "current_mtm": last_point["value"] if last_point else 0.0,
        "max_mtm": max_point["value"] if max_point else 0.0,
        "max_mtm_time": label(max_point),
        "min_mtm": min_point["value"] if min_point else 0.0,
        "min_mtm_time": label(min_point),
        "max_drawdown": _money(worst_drawdown),
        "pnl_series": pnl_series,
        "drawdown_series": drawdown_series,
        "source_point_count": count,
    }


def get_pnl_history(user_id, start_date=None, end_date=None):
    """Return inclusive IST date range P&L with truthful observation coverage.

    Raises ValueError for invalid ranges. Account balance fields are the current
    account, while chart values are change relative to the range-start baseline.
    Saved account realized/unrealized P&L takes precedence within snapshot
    coverage. A disclosed constant ledger offset aligns legacy execution-only
    history at the snapshot boundary without erasing later account changes.
    """
    now = datetime.now(IST)
    first_day = _parse_date(start_date, now.date())
    last_day = _parse_date(end_date, first_day)
    if last_day < first_day:
        raise ValueError("End date must be on or after start date.")
    if (last_day - first_day).days + 1 > MAX_RANGE_DAYS:
        raise ValueError("Choose a date range of 366 days or less.")
    if first_day > now.date():
        raise ValueError("Start date cannot be in the future.")
    if last_day > now.date():
        raise ValueError("End date cannot be in the future.")
    start = datetime.combine(first_day, time.min, IST)
    end = min(datetime.combine(last_day + timedelta(days=1), time.min, IST), now)
    start_utc = _utc_naive(start)
    end_utc = _utc_naive(end)
    lower_ms, upper_ms = _milliseconds(start), _milliseconds(end)
    notices = []
    with Session() as session:
        trades = (
            session.query(SandboxTrades)
            .filter(
                SandboxTrades.user_id == user_id,
                SandboxTrades.trade_timestamp < end.replace(tzinfo=None),
            )
            .order_by(SandboxTrades.trade_timestamp, SandboxTrades.id)
            .yield_per(1000)
        )
        replay = _replay_fills(trades, start, end)
        daily_rows = (
            session.query(SandboxDailyPnL)
            .filter(
                SandboxDailyPnL.user_id == user_id,
                SandboxDailyPnL.date >= first_day - timedelta(days=1),
                SandboxDailyPnL.date <= last_day,
            )
            .order_by(SandboxDailyPnL.date)
            .all()
        )
        valid_marks = []
        ignored_daily_records = 0
        baseline_unrealized = ZERO
        baseline_source = "realized_only"
        baseline_snapshot = (
            session.query(SandboxPnLSnapshot)
            .filter(
                SandboxPnLSnapshot.user_id == user_id,
                SandboxPnLSnapshot.timestamp <= start_utc,
            )
            .order_by(SandboxPnLSnapshot.timestamp.desc())
            .first()
        )
        if baseline_snapshot:
            baseline_unrealized = _decimal(baseline_snapshot.unrealized_pnl)
            baseline_source = "latest_observed_snapshot_before_start"
        for row in daily_rows:
            mark = _daily_mark(row)
            if mark is None:
                ignored_daily_records += 1
                continue
            if row.date == first_day - timedelta(days=1) and baseline_snapshot is None:
                baseline_unrealized = mark[1]
                baseline_source = "previous_recorded_eod_mark"
            elif lower_ms <= mark[0] <= upper_ms:
                valid_marks.append(mark)

        if not replay["carry_in"]:
            # Recorded executions prove that no contract is carried into the
            # range. An older aggregate mark must not survive completed exits.
            baseline_unrealized = ZERO
            baseline_source = "flat_at_start"

        snapshot_query = session.query(SandboxPnLSnapshot).filter(
            SandboxPnLSnapshot.user_id == user_id,
            SandboxPnLSnapshot.timestamp >= start_utc,
            SandboxPnLSnapshot.timestamp <= end_utc,
        )
        first_snapshot = snapshot_query.order_by(SandboxPnLSnapshot.timestamp).first()
        first_snapshot_ms = _milliseconds(_snapshot_time(first_snapshot.timestamp)) if first_snapshot else None
        fallback = [
            item for item in replay["events"]
            if first_snapshot_ms is None or item[0] < first_snapshot_ms
        ]
        for stamp, unrealized in valid_marks:
            if first_snapshot_ms is None or stamp < first_snapshot_ms:
                fallback.append((stamp, _realized_at(replay, stamp), unrealized, "recorded_eod_mark"))
        fallback.sort(key=lambda item: item[0])
        baseline = replay["baseline"] + baseline_unrealized
        # Explicit baseline anchor is known realized + most recent observed MTM.
        baseline_event = [(lower_ms, replay["baseline"], baseline_unrealized, "range_baseline")]
        account_offset = ZERO
        if baseline_snapshot:
            baseline_snapshot_ms = _milliseconds(_snapshot_time(baseline_snapshot.timestamp))
            account_offset = _decimal(baseline_snapshot.realized_pnl) - _realized_at(replay, baseline_snapshot_ms)
        elif first_snapshot:
            account_offset = _decimal(first_snapshot.realized_pnl) - _realized_at(replay, first_snapshot_ms)
        snapshot_stats = {
            "count": 0, "last_ms": None, "mismatches": 0, "large_gaps": 0,
            "last_realized": replay["end_realized"], "last_equity": None,
        }

        def snapshot_events():
            previous = None
            for row in snapshot_query.order_by(SandboxPnLSnapshot.timestamp, SandboxPnLSnapshot.id).yield_per(1000):
                stamp = _milliseconds(_snapshot_time(row.timestamp))
                reconstructed = _realized_at(replay, stamp)
                account_realized = _decimal(row.realized_pnl) - account_offset
                if abs(reconstructed - account_realized) > Decimal("0.05"):
                    snapshot_stats["mismatches"] += 1
                if previous is not None and stamp - previous > 60000:
                    snapshot_stats["large_gaps"] += 1
                previous = stamp
                snapshot_stats["count"] += 1
                snapshot_stats["last_ms"] = stamp
                snapshot_stats["last_realized"] = account_realized
                snapshot_stats["last_equity"] = _decimal(row.equity)
                yield stamp, account_realized, _decimal(row.unrealized_pnl), "recorded_live_snapshot"

        if first_snapshot is None and (
            not fallback or fallback[-1][3] == "executions_realized" or fallback[-1][2] == ZERO
        ):
            # End sample is realized only. A genuine nonzero EOD mark remains
            # at its observation timestamp instead of being erased by a fake
            # subsequent zero-MTM observation.
            fallback.append((upper_ms, replay["end_realized"], ZERO, "executions_realized"))
        timeline = merge(baseline_event, fallback, snapshot_events(), key=lambda item: item[0])
        data = _series_and_metrics(timeline, baseline, lower_ms, upper_ms)
        snapshots = snapshot_stats["count"]
        if snapshots:
            curve_type = "recorded_mtm" if not fallback else "mixed_realized_and_recorded_mtm"
        else:
            curve_type = "realized_with_recorded_eod" if valid_marks else "realized_only"
        if fallback or not snapshots:
            notices.append(
                "Past periods without saved intraday marks show realized P&L from executed trades. "
                "Recorded end-of-day marks are included where available; historical unrealized "
                "changes and their intraday peaks/drawdowns cannot be reconstructed."
            )
        if replay["carry_in"] and baseline_source == "realized_only":
            notices.append(
                "The range starts with carried positions but no recorded opening MTM. "
                "The baseline contains realized P&L only, so it is not an exact range equity return."
            )
        if baseline_snapshot:
            age = (start_utc - baseline_snapshot.timestamp).total_seconds()
            if age > 60 and baseline_unrealized != ZERO:
                notices.append("Opening unrealized P&L uses the last recorded mark before the start date.")
        if snapshot_stats["large_gaps"]:
            notices.append("Some saved snapshot intervals have gaps; the curve joins recorded observations.")
        if account_offset != ZERO:
            notices.append(
                "Legacy account P&L and reconstructed executions have a starting difference. "
                "Saved account marks are aligned to the historical execution baseline with a constant offset."
            )
        if snapshot_stats["mismatches"]:
            notices.append(
                "Some recorded account P&L changes are not present in executed trades, "
                "for example expiry settlement or a manual reset. The saved account values "
                "are retained within snapshot coverage."
            )
        if replay["invalid"]:
            notices.append("Some invalid legacy execution records were excluded from reconstruction.")
        funds = session.query(SandboxFunds).filter_by(user_id=user_id).first()
        account = None
        if funds:
            holdings = session.query(SandboxHoldings).filter_by(user_id=user_id).all()
            holding_pnl = sum((_decimal(row.pnl) for row in holdings if row.quantity), ZERO)
            holding_value = sum(
                (
                    _decimal(row.quantity)
                    * (_decimal(row.ltp) if _decimal(row.ltp) > 0 else _decimal(row.average_price))
                    for row in holdings if row.quantity
                ),
                ZERO,
            )
            account = {
                "initial_capital": _money(_decimal(funds.total_capital)),
                "available_balance": _money(_decimal(funds.available_balance)),
                "used_margin": _money(_decimal(funds.used_margin)),
                "realized_pnl": _money(_decimal(funds.realized_pnl)),
                "unrealized_pnl": _money(_decimal(funds.unrealized_pnl) + holding_pnl),
                "equity": _money(
                    _decimal(funds.available_balance) + _decimal(funds.used_margin)
                    + _decimal(funds.unrealized_pnl) + holding_value
                ),
            }
        days = []
        cursor = first_day
        while cursor <= min(last_day, now.date()):
            key = cursor.isoformat()
            days.append({
                "date": key,
                "realized_pnl": _money(replay["daily_realized"].get(key, ZERO)),
                "trade_count": replay["daily_trade_counts"].get(key, 0),
            })
            cursor += timedelta(days=1)
        data.update({
            "start_date": first_day.isoformat(),
            "end_date": last_day.isoformat(),
            "timezone": "Asia/Kolkata",
            "curve_type": curve_type,
            "notice": " ".join(notices),
            "coverage": {
                "snapshot_count": snapshots,
                "first_snapshot": first_snapshot_ms,
                "last_snapshot": snapshot_stats["last_ms"],
                "snapshot_gap_count": snapshot_stats["large_gaps"],
                "account_realized_offset": _money(account_offset),
                "non_execution_account_change_samples": snapshot_stats["mismatches"],
                "legacy_eod_marks": len(valid_marks),
                "unverified_legacy_daily_records": ignored_daily_records,
                "realized_execution_points": len(replay["events"]),
                "intraday_unrealized_available_before_first_snapshot": False,
                "baseline_source": baseline_source,
                "carry_in_contracts": replay["carry_in"],
            },
            "baseline": {
                "realized_pnl": _money(replay["baseline"]),
                "unrealized_pnl": _money(baseline_unrealized),
                "source": baseline_source,
            },
            "realized_pnl": _money(snapshot_stats["last_realized"] - replay["baseline"]),
            "execution_realized_pnl": _money(replay["end_realized"] - replay["baseline"]),
            "closing_balance": _money(snapshot_stats["last_equity"]) if snapshot_stats["last_equity"] is not None else None,
            "trade_count": replay["trade_count"],
            "daily_summary": days,
            "account": account,
        })
        if baseline_snapshot and 0 <= (start_utc - baseline_snapshot.timestamp).total_seconds() <= 30:
            data["opening_balance"] = _money(_decimal(baseline_snapshot.equity))
            data["opening_balance_observed_at"] = _milliseconds(_snapshot_time(baseline_snapshot.timestamp))
        return data

