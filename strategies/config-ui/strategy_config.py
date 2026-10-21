"""Shared defaults and validation for the strategy configuration page."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SUPPORTED_INDEXES = {
    "NIFTY": {"underlying_exchange": "NSE_INDEX", "option_exchange": "NFO"},
    "BANKNIFTY": {"underlying_exchange": "NSE_INDEX", "option_exchange": "NFO"},
    "FINNIFTY": {"underlying_exchange": "NSE_INDEX", "option_exchange": "NFO"},
    "MIDCPNIFTY": {"underlying_exchange": "NSE_INDEX", "option_exchange": "NFO"},
    "SENSEX": {"underlying_exchange": "BSE_INDEX", "option_exchange": "BFO"},
    "BANKEX": {"underlying_exchange": "BSE_INDEX", "option_exchange": "BFO"},
}

DEFAULT_CONFIGURATION: dict[str, Any] = {
    "settings": {
        "product": "NRML",
        "candle_minutes": 5,
        "stochrsi_rsi_period": 4,
        "stochrsi_lookback": 4,
        "stochrsi_k_smooth": 2,
        "stochrsi_d_smooth": 2,
        "entry_k": 0.0,
        "exit_k_level": 80.0,
        "stop_loss_points": 15.0,
        "entry_premium_min": 0.0,
        "entry_premium_max": 10000000.0,
    },
    "workers": [
        {
            "name": "THREAD-1",
            "underlying": "NIFTY",
            "expiry_date": "29SEP26",
            "strike_price": 22900,
            "option_type": "CE",
            "lots": 1,
        },
        {
            "name": "THREAD-2",
            "underlying": "NIFTY",
            "expiry_date": "29SEP26",
            "strike_price": 22950,
            "option_type": "PE",
            "lots": 1,
        },
    ],
}

_CANDLE_MINUTES = {1, 3, 5, 10, 15, 30}
AUTO_PREMIUM_CAP = 5000.0
AUTO_SIDES = ("CE", "PE")
CAPTURE_MODES = ("single", "all")
CAPTURE_LIMITS = ("count", "balance")   # Max positions, or Until account balance allows
CAPTURE_HARD_CAP = 10       # most strike workers one side may run in Capture all
_EXPIRY_FORMAT = re.compile(r"^\d{2}[A-Z]{3}\d{2}$")


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive whole number")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive whole number") from exc
    if number <= 0 or str(value).strip() not in {str(number), f"{number}.0"}:
        raise ValueError(f"{label} must be a positive whole number")
    return number


def _number(value: Any, label: str, *, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if number != number or number in {float("inf"), float("-inf")}:  # Reject NaN/infinity.
        raise ValueError(f"{label} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} must be at least {minimum:g}")
    return number


def normalize_configuration(value: Any) -> dict[str, Any]:
    """Validate and return the canonical settings/workers JSON shape.

    A legacy top-level worker array remains readable so existing local config
    files can be loaded and then saved in the new shape by the UI.
    """
    if isinstance(value, list):
        legacy_workers = []
        default_workers = DEFAULT_CONFIGURATION["workers"]
        for index, worker in enumerate(value):
            if not isinstance(worker, dict):
                raise ValueError(f"Worker {index + 1} must be an object")
            worker_defaults = default_workers[index % len(default_workers)]
            legacy_workers.append({**worker_defaults, **worker})
        value = {"workers": legacy_workers}
    if not isinstance(value, dict):
        raise ValueError("Configuration must be an object with settings and workers")

    settings_in = value.get("settings", {})
    workers_in = value.get("workers", DEFAULT_CONFIGURATION["workers"])
    if not isinstance(settings_in, dict):
        raise ValueError("settings must be an object")
    if not isinstance(workers_in, list):
        raise ValueError("workers must be a list")

    settings = dict(DEFAULT_CONFIGURATION["settings"])
    settings.update(settings_in)

    product = str(settings.get("product", "NRML")).strip().upper()
    if product not in {"MIS", "NRML"}:
        raise ValueError("Product must be MIS or NRML")
    settings["product"] = product

    candle_minutes = _positive_int(settings["candle_minutes"], "Candle interval")
    if candle_minutes not in _CANDLE_MINUTES:
        raise ValueError("Candle interval must be 1, 3, 5, 10, 15, or 30 minutes")
    settings["candle_minutes"] = candle_minutes

    for field in (
        "stochrsi_rsi_period",
        "stochrsi_lookback",
        "stochrsi_k_smooth",
        "stochrsi_d_smooth",
    ):
        settings[field] = _positive_int(settings[field], field.replace("_", " "))

    settings["entry_k"] = _number(settings["entry_k"], "Entry K", minimum=0)
    settings["exit_k_level"] = _number(settings["exit_k_level"], "Exit K", minimum=0)
    if settings["entry_k"] > 100 or settings["exit_k_level"] > 100:
        raise ValueError("StochRSI K levels must be between 0 and 100")
    settings["stop_loss_points"] = _number(settings["stop_loss_points"], "Stop loss", minimum=0)
    settings["entry_premium_min"] = _number(settings["entry_premium_min"], "Minimum premium", minimum=0)
    settings["entry_premium_max"] = _number(settings["entry_premium_max"], "Maximum premium", minimum=0)
    if settings["entry_premium_min"] > settings["entry_premium_max"]:
        raise ValueError("Minimum premium cannot exceed maximum premium")

    workers: list[dict[str, Any]] = []
    worker_names: set[str] = set()
    contracts: set[tuple[str, str, int, str]] = set()
    for index, worker_in in enumerate(workers_in, start=1):
        if not isinstance(worker_in, dict):
            raise ValueError(f"Worker {index} must be an object")
        name = str(worker_in.get("name", f"THREAD-{index}")).strip()
        if not name:
            raise ValueError(f"Worker {index} needs a name")
        if name.casefold() in worker_names:
            raise ValueError(f"Worker name {name!r} is duplicated")
        worker_names.add(name.casefold())

        underlying = str(worker_in.get("underlying", "NIFTY")).strip().upper()
        if underlying not in SUPPORTED_INDEXES:
            raise ValueError(f"{name}: choose a supported index")
        expiry = str(worker_in.get("expiry_date", "")).strip().upper()
        if not _EXPIRY_FORMAT.fullmatch(expiry):
            raise ValueError(f"{name}: expiry must use DDMMMYY, for example 29SEP26")
        strike = _positive_int(worker_in.get("strike_price"), f"{name} strike")
        option_type = str(worker_in.get("option_type", "")).strip().upper()
        if option_type not in {"CE", "PE"}:
            raise ValueError(f"{name}: option type must be CE or PE")
        lots = _positive_int(worker_in.get("lots"), f"{name} lots")
        enabled = worker_in.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{name}: enabled must be true or false")
        worker_candle_minutes = _positive_int(
            worker_in.get("candle_minutes", settings["candle_minutes"]), f"{name} candle interval"
        )
        if worker_candle_minutes not in _CANDLE_MINUTES:
            raise ValueError(f"{name}: candle interval must be 1, 3, 5, 10, 15, or 30 minutes")

        contract = (underlying, expiry, strike, option_type)
        if enabled and contract in contracts:
            raise ValueError(f"{name}: this index/expiry/strike/type leg is already enabled")
        if enabled:
            contracts.add(contract)
        workers.append(
            {
                "name": name,
                "underlying": underlying,
                "underlying_exchange": SUPPORTED_INDEXES[underlying]["underlying_exchange"],
                "option_exchange": SUPPORTED_INDEXES[underlying]["option_exchange"],
                "expiry_date": expiry,
                "strike_price": strike,
                "option_type": option_type,
                "lots": lots,
                "enabled": enabled,
                "candle_minutes": worker_candle_minutes,
            }
        )

    mode = str(value.get("mode", "manual")).lower()
    if mode not in {"manual", "automatic"}:
        raise ValueError("Choose Manual or Automatic mode")
    auto_in = value.get("automatic", {})
    if not isinstance(auto_in, dict):
        raise ValueError("Automatic settings must be an object")
    auto = {"underlying": "NIFTY", "expiry_date": workers[0]["expiry_date"] if workers else "29SEP26", "lots": 1,
            "candle_minutes": settings["candle_minutes"], "premium_min": 100.0,
            "premium_max": 200.0, "max_levels": 15, "enabled": False, **auto_in}
    auto["underlying"] = str(auto["underlying"]).upper().strip()
    if auto["underlying"] not in SUPPORTED_INDEXES:
        raise ValueError("Automatic mode: choose a supported index")
    auto["expiry_date"] = str(auto["expiry_date"]).upper().strip()
    if not _EXPIRY_FORMAT.fullmatch(auto["expiry_date"]):
        raise ValueError("Automatic mode: choose an expiry")
    auto["lots"] = _positive_int(auto["lots"], "Automatic lots")
    auto["candle_minutes"] = _positive_int(auto["candle_minutes"], "Automatic candle interval")
    if auto["candle_minutes"] not in _CANDLE_MINUTES:
        raise ValueError("Automatic mode: unsupported candle interval")
    auto["premium_min"] = _number(auto["premium_min"], "Automatic minimum premium", minimum=0.05)
    auto["premium_max"] = _number(auto["premium_max"], "Automatic maximum premium", minimum=0.05)
    if auto["premium_max"] > AUTO_PREMIUM_CAP:
        raise ValueError(f"Automatic maximum premium cannot exceed {AUTO_PREMIUM_CAP:g}")
    if auto["premium_min"] >= auto["premium_max"]:
        raise ValueError("Automatic maximum premium must be above the minimum")
    auto["max_levels"] = _positive_int(auto["max_levels"], "Automatic search levels")
    if auto["max_levels"] > 15:
        raise ValueError("Automatic search supports at most 15 levels in each direction")
    if not isinstance(auto["enabled"], bool):
        raise ValueError("Automatic enabled must be true or false")
    # Per-side run flags. Older files only have "enabled", which applied to both sides.
    for side in AUTO_SIDES:
        key = "enabled_" + side.lower()
        auto[key] = auto.get(key, auto["enabled"])
        if not isinstance(auto[key], bool):
            raise ValueError(f"Automatic {side} enabled must be true or false")
    auto["enabled"] = any(auto["enabled_" + side.lower()] for side in AUTO_SIDES)
    # Capture mode per side (Single = one strike, All = every strike in the band) and its entry limit.
    for side in AUTO_SIDES:
        key = "capture_" + side.lower()
        auto[key] = str(auto.get(key, "single")).lower()
        if auto[key] not in CAPTURE_MODES:
            raise ValueError(f"Automatic {side} mode must be Single or Capture all")
    auto["capture_limit"] = str(auto.get("capture_limit", "count")).lower()
    if auto["capture_limit"] not in CAPTURE_LIMITS:
        raise ValueError("Capture all limit must be Max positions or Until balance allows")
    auto["capture_max_entries"] = _positive_int(auto.get("capture_max_entries", 3), "Max positions")
    if auto["capture_max_entries"] > CAPTURE_HARD_CAP:
        raise ValueError(f"Max positions cannot exceed {CAPTURE_HARD_CAP}")
    auto.pop("capture_budget", None)
    return {"settings": settings, "workers": workers, "mode": mode, "automatic": auto,
            "pairs": _normalize_pairs(value.get("pairs", []), workers)}


def _normalize_pairs(pairs_in, workers):
    """Manual CE/PE pairs as [[ce_name, pe_name], ...]. Pairs whose thread was deleted drop out."""
    if not isinstance(pairs_in, list):
        raise ValueError("pairs must be a list")
    rows = {w["name"]: w for w in workers}
    pairs, used = [], set()
    for pair in pairs_in:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("Each pair needs one CE and one PE thread")
        a, b = (str(x) for x in pair)
        if a not in rows or b not in rows:
            continue
        ce, pe = (a, b) if rows[a]["option_type"] == "CE" else (b, a)
        if rows[ce]["option_type"] != "CE" or rows[pe]["option_type"] != "PE":
            raise ValueError("A pair must be one CE thread and one PE thread")
        if rows[ce]["underlying"] != rows[pe]["underlying"]:
            raise ValueError("Both threads in a pair must be on the same index")
        if ce in used or pe in used:
            raise ValueError("A thread can be in only one pair")
        used.update((ce, pe))
        pairs.append([ce, pe])
    return pairs


def pair_partner(config, name):
    for ce, pe in config.get("pairs", []):
        if name == ce:
            return pe
        if name == pe:
            return ce
    return None


def add_pair(config, ce, pe):
    if config["mode"] != "manual":
        raise ValueError("Pairing is for Manual mode")
    if pair_partner(config, ce) or pair_partner(config, pe):
        raise ValueError("One of these threads is already paired; unpair it first")
    config["pairs"] = _normalize_pairs(config.get("pairs", []) + [[ce, pe]], config["workers"])
    if [ce, pe] not in config["pairs"] and [pe, ce] not in config["pairs"]:
        raise ValueError("Choose two existing threads: one CE and one PE")
    return config


def remove_pair(config, name):
    config["pairs"] = [p for p in config.get("pairs", []) if name not in p]
    return config


def set_automatic_sides(config, sides, enabled, capture=None):
    """Set the run flag (and optionally the capture mode) for the given automatic sides."""
    if capture is not None and capture not in CAPTURE_MODES:
        raise ValueError("Choose Single or Capture all")
    for side in sides:
        config["automatic"]["enabled_" + side.lower()] = bool(enabled)
        if capture is not None:
            config["automatic"]["capture_" + side.lower()] = capture
    config["automatic"]["enabled"] = any(config["automatic"]["enabled_" + side.lower()] for side in AUTO_SIDES)
    return config


def keep_saved_run_flags(config, current):
    """Run flags and capture modes change only through Start/Stop; a settings save keeps the saved ones."""
    for key in ("enabled", *("enabled_" + side.lower() for side in AUTO_SIDES),
                *("capture_" + side.lower() for side in AUTO_SIDES)):
        config["automatic"][key] = current["automatic"][key]
    return config


def guard_automatic_edit(config, current):
    """Automatic settings can change only while both sides are Off (not armed, not running)."""
    run_keys = {"enabled", *("enabled_" + side.lower() for side in AUTO_SIDES), *("capture_" + side.lower() for side in AUTO_SIDES)}
    strip = lambda auto: {k: v for k, v in auto.items() if k not in run_keys}
    on = [side for side in AUTO_SIDES if current["automatic"]["enabled_" + side.lower()]]
    if on and strip(config["automatic"]) != strip(current["automatic"]):
        raise ValueError("Automatic settings are locked while " + " and ".join(on) + (" is" if len(on) == 1 else " are")
                         + " on. Set both sides to Off to edit them.")


def requested_sides(value):
    sides = value if value is not None else list(AUTO_SIDES)
    if not isinstance(sides, list) or not sides or any(side not in AUTO_SIDES for side in sides):
        raise ValueError("Choose CE, PE or both")
    return sorted(set(sides), key=AUTO_SIDES.index)


def read_configuration(path: Path) -> dict[str, Any]:
    if not path.exists():
        return normalize_configuration(DEFAULT_CONFIGURATION)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    return normalize_configuration(loaded)


def automatic_rows(config):
    auto = config["automatic"]
    exchanges = SUPPORTED_INDEXES[auto["underlying"]]
    return [{"name": "AUTO-" + side, "underlying": auto["underlying"], **exchanges,
             "expiry_date": auto["expiry_date"], "strike_price": 0, "option_type": side,
             "lots": auto["lots"], "candle_minutes": auto["candle_minutes"],
             "enabled": auto["enabled_" + side.lower()], "automatic": True,
             "premium_min": auto["premium_min"], "premium_max": auto["premium_max"],
             "max_levels": auto["max_levels"], "capture": auto["capture_" + side.lower()],
             "capture_limit": auto["capture_limit"], "capture_max_entries": auto["capture_max_entries"]}
            for side in AUTO_SIDES]


def new_manual_row(config, value):
    """A manual thread from the quick Add box: contract + lots. Name is generated, shared rules apply."""
    if not isinstance(value, dict):
        raise ValueError("Choose a contract to add")
    names = {r["name"].casefold() for r in config["workers"]}
    index = 1
    while f"THREAD-{index}".casefold() in names:
        index += 1
    row = {"name": f"THREAD-{index}", "underlying": value.get("underlying"), "expiry_date": value.get("expiry_date"),
           "strike_price": value.get("strike_price"), "option_type": value.get("option_type"),
           "lots": value.get("lots"), "candle_minutes": config["settings"]["candle_minutes"], "enabled": False}
    # Validate through the normal path, then refuse a contract that already has a thread.
    checked = normalize_configuration({**config, "workers": [row]})["workers"][0]
    key = lambda r: (r["underlying"], r["expiry_date"], int(r["strike_price"]), r["option_type"])
    if any(key(r) == key(checked) for r in config["workers"]):
        raise ValueError(f"{checked['strike_price']} {checked['option_type']} {checked['expiry_date']} already has a thread")
    return checked
