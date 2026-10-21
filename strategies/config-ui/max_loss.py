"""Max loss today: one loss limit for the whole SRSI strategy (manual and automatic threads together).

Today's P&L = booked P&L of exits filled today + open P&L of positions held now. A position carried
from an earlier day counts from the first price seen today, so yesterday's move never counts today.
When the loss reaches the limit the day is tripped: every leg stops buying and sells what it holds,
every thread is turned off, nothing can start again until the next day, and an email is sent.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from runtime_control import atomic_json, config_lock

BASE = Path(__file__).resolve().parent
RISK_FILE = BASE / "risk.json"
DAY_FILE = BASE / "max_loss_day.json"
MAILER = BASE / "max_loss_mailer.py"
OPENALGO_DIR = Path("/home/ubuntu/openalgo")
OPENALGO_PYTHON = OPENALGO_DIR / "venv" / "bin" / "python"
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN, MARKET_CLOSE = dt_time(9, 15), dt_time(15, 30)
MAX_EMAILS = 10
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
DEFAULT_RISK = {"enabled": False, "amount": 3000.0, "emails": []}

# Set while today is tripped. Every leg checks it: no new buys, and sell whatever is held.
TRIPPED = threading.Event()
TRIP_REASON = "MAX_LOSS_TODAY"
_LOCK = threading.RLock()
_DAY = None
CURRENT_MODE = None   # "live" or "analyze", set by the supervisor; each mode keeps its own day


def today():
    return datetime.now(IST).date().isoformat()


# ----- settings ----------------------------------------------------------------------------------

def normalize_risk(value):
    if not isinstance(value, dict):
        raise ValueError("Max loss settings must be an object")
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("Max loss on/off must be true or false")
    try:
        amount = float(value.get("amount", DEFAULT_RISK["amount"]))
    except (TypeError, ValueError):
        raise ValueError("Enter the max loss as a number of rupees") from None
    if not math.isfinite(amount) or amount < 100:
        raise ValueError("Max loss must be at least ₹100")
    if amount > 10_000_000:
        raise ValueError("Max loss is too large")
    emails = value.get("emails", [])
    if isinstance(emails, str):
        emails = re.split(r"[\s,;]+", emails)
    if not isinstance(emails, list):
        raise ValueError("Alert emails must be a list")
    cleaned = []
    for email in emails:
        email = str(email).strip()
        if not email:
            continue
        if not EMAIL_RE.match(email) or len(email) > 254:
            raise ValueError(f"{email} is not a valid email address")
        if email.lower() not in {e.lower() for e in cleaned}:
            cleaned.append(email)
    if len(cleaned) > MAX_EMAILS:
        raise ValueError(f"Add at most {MAX_EMAILS} alert emails")
    return {"enabled": enabled, "amount": round(amount, 2), "emails": cleaned}


def read_risk():
    try:
        return normalize_risk(json.loads(RISK_FILE.read_text()))
    except (OSError, ValueError):
        return dict(DEFAULT_RISK)


def save_risk(value):
    risk = normalize_risk(value)
    with config_lock():
        atomic_json(RISK_FILE, risk)
    return risk


# ----- today's state -----------------------------------------------------------------------------

def _fresh():
    return {"date": today(), "mode": CURRENT_MODE, "realized": 0.0, "basis": {}, "tripped": False, "tripped_at": None,
            "trip_pnl": None, "trip_limit": None, "worst": 0.0, "emails": {}, "closed_at": None}


def _state():
    """Caller holds _LOCK. Load once, start over on a new day, and keep TRIPPED in step."""
    global _DAY
    if _DAY is None:
        try:
            _DAY = json.loads(DAY_FILE.read_text())
        except (OSError, ValueError):
            _DAY = None
    if (not isinstance(_DAY, dict) or _DAY.get("date") != today()
            or (CURRENT_MODE and _DAY.get("mode") not in (None, CURRENT_MODE))):
        # New day, or OpenAlgo switched Analyze <-> Live: paper and real money never share a total.
        _DAY = _fresh()
        _save()
    elif CURRENT_MODE and _DAY.get("mode") is None:
        _DAY["mode"] = CURRENT_MODE
        _save()
    if _DAY.get("tripped"):
        TRIPPED.set()
    else:
        TRIPPED.clear()
    return _DAY


def _save():
    try:
        atomic_json(DAY_FILE, _DAY)
    except Exception:
        pass


def set_mode(mode):
    global CURRENT_MODE
    if mode not in ("live", "analyze"):
        return
    with _LOCK:
        CURRENT_MODE = mode
        _state()


def is_tripped():
    with _LOCK:
        return bool(_state()["tripped"])


def on_buy(symbol, quantity, price):
    """A buy filled today: its own price is the basis for today's P&L."""
    if quantity <= 0 or not price or price <= 0:
        return
    with _LOCK:
        st = _state()
        rec = st["basis"].get(symbol)
        if rec and rec["qty"] > 0:
            total = rec["qty"] + quantity
            rec["price"] = round((rec["price"] * rec["qty"] + price * quantity) / total, 4)
            rec["qty"] = total
        else:
            st["basis"][symbol] = {"price": float(price), "qty": int(quantity)}
        _save()


def on_sell(symbol, quantity, price, fallback_entry=0.0):
    """An exit filled: book (exit - today's basis) × quantity."""
    if quantity <= 0 or not price or price <= 0:
        return
    with _LOCK:
        st = _state()
        rec = st["basis"].get(symbol)
        basis = rec["price"] if rec else float(fallback_entry or price)
        st["realized"] = round(st["realized"] + (float(price) - basis) * quantity, 2)
        if rec:
            rec["qty"] = max(0, rec["qty"] - quantity)
            if rec["qty"] == 0:
                del st["basis"][symbol]
        _save()


def leaf_legs(worker):
    """The OptionLegWorker objects that hold positions, for any worker kind."""
    if hasattr(worker, "_children"):
        return list(worker._children())
    if hasattr(worker, "engine"):
        return [worker.child] if getattr(worker, "child", None) else []
    return [worker]


def evaluate(workers):
    """Recompute today's P&L from live legs. Returns (snapshot, just_tripped)."""
    risk = read_risk()
    now = datetime.now(IST)
    in_market = MARKET_OPEN <= now.time() < MARKET_CLOSE
    with _LOCK:
        st = _state()
        unrealized, positions, changed = 0.0, [], False
        for worker in workers:
            for leg in leaf_legs(worker):
                state = getattr(leg, "state", None)
                qty = int(getattr(state, "position_quantity", 0) or 0)
                ltp = getattr(state, "last_ltp", None)
                if qty <= 0:
                    continue
                symbol = leg.symbol
                rec = st["basis"].get(symbol)
                if rec is None or rec["qty"] <= 0:
                    if not ltp:
                        continue
                    # Carried from an earlier day (or recovered): count from the first price seen today.
                    rec = st["basis"][symbol] = {"price": float(ltp), "qty": qty, "carried": True}
                    changed = True
                elif rec["qty"] != qty:
                    rec["qty"] = qty
                    changed = True
                open_pnl = (float(ltp) - rec["price"]) * qty if ltp else 0.0
                unrealized += open_pnl
                positions.append({"symbol": symbol, "qty": qty, "basis": round(rec["price"], 2),
                                  "ltp": ltp, "pnl": round(open_pnl, 2), "halted": bool(getattr(state, "halted", False)),
                                  "phase": getattr(state, "phase", "")})
        pnl = round(st["realized"] + unrealized, 2)
        if pnl < st["worst"]:
            st["worst"] = pnl
            changed = True
        just_tripped = False
        if (risk["enabled"] and not st["tripped"] and in_market and pnl <= -risk["amount"]):
            st.update(tripped=True, tripped_at=now.isoformat(timespec="seconds"), trip_pnl=pnl, trip_limit=risk["amount"])
            TRIPPED.set()
            just_tripped = changed = True
        if changed:
            _save()
        snapshot = {"date": st["date"], "mode": st.get("mode"), "enabled": risk["enabled"], "amount": risk["amount"], "emails": risk["emails"],
                    "realized": round(st["realized"], 2), "unrealized": round(unrealized, 2), "pnl": pnl,
                    "worst": st["worst"], "tripped": st["tripped"], "tripped_at": st["tripped_at"],
                    "trip_pnl": st["trip_pnl"], "trip_limit": st["trip_limit"], "positions": positions,
                    "closed_at": st["closed_at"], "email_status": st["emails"], "live": True}
        return snapshot, just_tripped


def offline_snapshot():
    """For the page while SRSI is stopped: today's saved state, no live prices."""
    risk = read_risk()
    with _LOCK:
        try:
            st = json.loads(DAY_FILE.read_text())
        except (OSError, ValueError):
            st = None
        if not isinstance(st, dict) or st.get("date") != today():
            st = _fresh()
    return {"date": st["date"], "enabled": risk["enabled"], "amount": risk["amount"], "emails": risk["emails"],
            "realized": st["realized"], "unrealized": 0.0, "pnl": st["realized"], "worst": st["worst"],
            "tripped": st["tripped"], "tripped_at": st["tripped_at"], "trip_pnl": st["trip_pnl"],
            "trip_limit": st["trip_limit"], "positions": [], "closed_at": st["closed_at"],
            "email_status": st["emails"], "live": False}


def mark_closed():
    """All positions are flat after a trip. Returns True the first time."""
    with _LOCK:
        st = _state()
        if not st["tripped"] or st["closed_at"]:
            return False
        st["closed_at"] = datetime.now(IST).isoformat(timespec="seconds")
        _save()
        return True


# ----- email -------------------------------------------------------------------------------------

def _record_email(kind, result):
    with _LOCK:
        st = _state()
        st["emails"][kind] = result
        _save()


def send_alert(kind, subject, text, logger=print, html=None):
    """Send through OpenAlgo's SMTP (admin page settings) in the background; never blocks trading."""
    emails = read_risk()["emails"]
    if not emails:
        _record_email(kind, {"sent": [], "failed": [], "note": "no alert emails set"})
        return

    def work():
        python = str(OPENALGO_PYTHON) if OPENALGO_PYTHON.exists() else sys.executable
        try:
            done = subprocess.run([python, "-E", "-s", str(MAILER)], cwd=str(OPENALGO_DIR), timeout=90,
                                  input=json.dumps({"to": emails, "subject": subject, "text": text, "html": html}),
                                  capture_output=True, text=True)
            lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
            result = json.loads(lines[-1]) if lines else {"sent": [], "failed": emails, "note": f"mailer exit {done.returncode}"}
        except Exception as exc:
            result = {"sent": [], "failed": emails, "note": type(exc).__name__}
        result["at"] = datetime.now(IST).isoformat(timespec="seconds")
        _record_email(kind, result)
        logger(f"[MAX LOSS] {kind} email sent={result.get('sent')} failed={result.get('failed')} {result.get('note', '')}")

    threading.Thread(target=work, name=f"max-loss-email-{kind}", daemon=True).start()


def rupees(value):
    sign = "-" if value < 0 else ""
    return f"{sign}₹{abs(value):,.2f}"
