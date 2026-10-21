"""One Resend email per uninterrupted excursion outside [100, 300]."""
from __future__ import annotations
import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
CONFIG = BASE / 'price-alerts.private.json'
DB = BASE / 'price-alerts.sqlite3'
_LOCK = threading.Lock()
_STARTED = False
_DISABLED_LOGGED = False
_LAST_ERROR_LOG = 0.0


def _settings():
    try:
        settings = json.loads(CONFIG.read_text())
        if settings.get('enabled') is False or not all(settings.get(k) for k in ('api_key', 'from', 'to')):
            return None
        return settings
    except (OSError, ValueError):
        return None


@contextmanager
def _connect():
    db = sqlite3.connect(DB, timeout=2)
    db.execute('CREATE TABLE IF NOT EXISTS band_state (worker TEXT PRIMARY KEY, outside INTEGER NOT NULL, last_alert REAL NOT NULL DEFAULT 0)')
    columns = {row[1] for row in db.execute('PRAGMA table_info(band_state)')}
    if 'last_alert' not in columns:
        db.execute('ALTER TABLE band_state ADD COLUMN last_alert REAL NOT NULL DEFAULT 0')
    db.execute('CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0)')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def observe_price_alert(worker, symbol, ltp, logger):
    """Never let email/config/storage errors interrupt trading decisions."""
    global _STARTED, _DISABLED_LOGGED, _LAST_ERROR_LOG
    try:
        if not math.isfinite(ltp) or ltp <= 0:
            return
        settings = _settings()
        if settings is None:
            with _LOCK:
                if not _DISABLED_LOGGED:
                    logger.info('[PRICE ALERT] Not enabled: complete price-alerts.private.json')
                    _DISABLED_LOGGED = True
            return
        with _LOCK:
            with _connect() as db:
                previous = db.execute('SELECT outside,last_alert FROM band_state WHERE worker=?', (worker,)).fetchone()
                outside = ltp < 100 or ltp > 300
                if not outside:
                    if previous and previous[0]:
                        db.execute('UPDATE band_state SET outside=0 WHERE worker=?', (worker,))
                elif not previous or not previous[0] or time.time() - previous[1] >= 300:
                    db.execute('INSERT INTO band_state(worker,outside,last_alert) VALUES (?,1,?) ON CONFLICT(worker) DO UPDATE SET outside=1,last_alert=excluded.last_alert', (worker,time.time()))
                    at = datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(timespec='seconds')
                    direction = 'below 100' if ltp < 100 else 'above 300'
                    recipients = settings['to'] if isinstance(settings['to'], list) else [settings['to']]
                    payload = {'from': settings['from'], 'to': recipients,
                        'subject': f'{worker}: {symbol} LTP {direction}',
                        'text': f'Thread: {worker}\nContract: {symbol}\nLTP: INR {ltp:.2f}\nTime: {at}\n\nAlerts repeat at most every 5 minutes while outside INR 100–300. They stop and re-arm after LTP returns to band. Trading rules are unchanged.'}
                    db.execute('INSERT INTO outbox(id,payload,state) VALUES (?,?,?)', (uuid.uuid4().hex, json.dumps(payload), 'pending'))
                    logger.info('[PRICE ALERT] %s %s LTP=%.2f queued (5-minute cooldown)', worker, symbol, ltp)
            if not _STARTED:
                threading.Thread(target=_deliver, args=(logger,), name='resend-price-alerts', daemon=True).start()
                _STARTED = True
    except Exception as exc:
        if time.monotonic() - _LAST_ERROR_LOG > 60:
            logger.warning('[PRICE ALERT] Observe failed (%s); trading continues', type(exc).__name__)
            _LAST_ERROR_LOG = time.monotonic()


def _deliver(logger):
    while True:
        try:
            settings = _settings()
            if settings:
                with _connect() as db:
                    item = db.execute("SELECT id,payload,attempts FROM outbox WHERE state='pending' AND next_try<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
                if item:
                    alert_id, payload, attempts = item
                    success, retryable, detail = False, True, 'connection error'
                    try:
                        request = Request('https://api.resend.com/emails', data=payload.encode(), method='POST', headers={
                            'Authorization': 'Bearer ' + settings['api_key'], 'Content-Type': 'application/json',
                            'Idempotency-Key': 'openalgo-price-' + alert_id, 'User-Agent': 'OpenAlgo-Price-Alerts/1.0'})
                        with urlopen(request, timeout=8) as response:
                            result = json.loads(response.read())
                            success = bool(result.get('id'))
                            detail = 'accepted' if success else 'missing email id'
                    except HTTPError as exc:
                        retryable = exc.code == 429 or exc.code >= 500
                        detail = f'HTTP {exc.code}'
                    except Exception as exc:
                        detail = type(exc).__name__
                    attempts += 1
                    state = 'sent' if success else 'pending' if retryable and attempts < 3 else 'failed'
                    with _connect() as db:
                        db.execute('UPDATE outbox SET state=?,attempts=?,next_try=? WHERE id=?', (state, attempts, time.time()+15*attempts, alert_id))
                    logger.info('[PRICE ALERT] email %s (%s)', state, detail)
        except Exception as exc:
            logger.warning('[PRICE ALERT] Delivery worker error (%s)', type(exc).__name__)
            time.sleep(30)
        time.sleep(1)
