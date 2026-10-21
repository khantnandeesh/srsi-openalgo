"""Read-only, bounded SRSI research. Never calls an order API."""
import importlib.util
import math
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from threading import Thread, Lock
from flask import request
from flask_restx import Namespace, Resource
from sqlalchemy import select
from database.auth_db import verify_api_key
from database.historify_db import get_connection
from database.symbol import SymToken, engine
from services.history_service import get_history
from limiter import limiter
from openalgo import ta
import numpy as np

api = Namespace('srsi-backtest', description='Minute-data SRSI research; no orders')
ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('backtest_incremental_srsi', ROOT / 'strategies/config-ui/incremental_srsi.py')
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
IncrementalSRSI = _mod.IncrementalSRSI
IST = ZoneInfo('Asia/Kolkata')
PATTERN = re.compile(r'^([A-Z]+)(\d{2}[A-Z]{3}\d{2})(\d+(?:\.\d+)?)(CE|PE)$')
INDEXES = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}
JOBS = {}
ACTIVE = False
LOCK = Lock()
DEFAULTS = dict(candle_minutes=5, stochrsi_rsi_period=4, stochrsi_lookback=4,
                stochrsi_k_smooth=2, stochrsi_d_smooth=2, entry_k=0,
                exit_k_level=80, stop_loss_points=15, entry_premium_min=0,
                entry_premium_max=10000000)


def catalog():
    contracts = {}
    with engine.connect() as connection:
        rows = connection.execute(select(SymToken.symbol, SymToken.exchange, SymToken.lotsize)
                                  .where(SymToken.exchange.in_(['NFO', 'BFO']))).all()
    for symbol, exchange, lot in rows:
        match = PATTERN.match(symbol)
        if match and match[1] in INDEXES:
            contracts[(symbol, exchange)] = dict(symbol=symbol, exchange=exchange,
                underlying=match[1], expiry=match[2], strike=float(match[3]),
                option_type=match[4], lot_size=int(lot or 0), archived=False, available_from=None, available_to=None)
    with get_connection() as connection:
        rows = connection.execute("SELECT symbol, exchange, min(timestamp), max(timestamp), count(*) FROM market_data WHERE interval='1m' AND exchange IN ('NFO','BFO') GROUP BY symbol,exchange").fetchall()
    for symbol, exchange, first, last, count in rows:
        match = PATTERN.match(symbol)
        if not match or match[1] not in INDEXES:
            continue
        item = contracts.setdefault((symbol, exchange), dict(symbol=symbol, exchange=exchange,
            underlying=match[1], expiry=match[2], strike=float(match[3]), option_type=match[4], lot_size=0))
        item.update(archived=True, available_from=datetime.fromtimestamp(first, IST).date().isoformat(),
                    available_to=datetime.fromtimestamp(last, IST).date().isoformat(), bars=count)
    defaults = DEFAULTS.copy()
    try:
        import json
        saved = json.loads((ROOT / 'strategies/config-ui/workers.json').read_text())
        defaults.update({k: v for k, v in saved.get('settings', {}).items() if k in defaults})
    except (OSError, ValueError):
        pass
    return dict(contracts=sorted(contracts.values(), key=lambda x: (x['underlying'], x['expiry'], x['strike'], x['option_type'])), defaults=defaults)


def whole_number(value, label):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{label} must be a whole number.') from None
    if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer():
        raise ValueError(f'{label} must be a whole number.')
    return int(number)


def validate(data):
    start = datetime.strptime(data['start'], '%Y-%m-%d').date()
    end = datetime.strptime(data['end'], '%Y-%m-%d').date()
    if not 0 <= (end - start).days <= 366 or end > datetime.now(IST).date():
        raise ValueError('Choose a past date range of at most 366 days.')
    automatic = data.get('selection_mode') == 'daily'
    catalog_contracts = catalog()['contracts']
    items = data.get('contracts', [])
    sides = data.get('sides', ['CE', 'PE'])
    if automatic:
        if not isinstance(sides, list) or not sides or len(set(sides)) != len(sides) or any(side not in ('CE', 'PE') for side in sides):
            raise ValueError('Choose call, put, or both.')
        candidates = [c for c in catalog_contracts if c['underlying'] == data.get('underlying') and c['expiry'] == data.get('expiry') and c['option_type'] in sides]
        if not candidates:
            raise ValueError('No contracts available for this underlying and expiry.')
        items = [dict(symbol=c['symbol'], exchange=c['exchange'], lot_size=data.get('lot_size') or c['lot_size']) for c in candidates]
    if not isinstance(items, list) or not 1 <= len(items) <= (1500 if automatic else 2):
        raise ValueError('Select one or two contracts.')
    known = {(c['symbol'], c['exchange']): c for c in catalog_contracts}
    selected = []
    for item in items:
        contract = known.get((item.get('symbol'), item.get('exchange')))
        if not contract:
            raise ValueError('Contract is not in the master or Historify archive.')
        lot = whole_number(item.get('lot_size') or contract['lot_size'], 'Units per lot')
        if not 1 <= lot <= 10000:
            raise ValueError('Enter the historical lot size for archived contracts without metadata.')
        if end > datetime.strptime(contract['expiry'], '%d%b%y').date():
            raise ValueError('End date must be on or before the selected expiry.')
        selected.append({**contract, 'lot_size': lot})
    if len({(c['underlying'], c['expiry']) for c in selected}) != 1 or (not automatic and len({c['option_type'] for c in selected}) != len(selected)):
        raise ValueError('Choose at most one call and one put of the same underlying and expiry.')
    lots = whole_number(data.get('lots', 1), 'Lots')
    if not 1 <= lots <= 1000:
        raise ValueError('Lots must be between 1 and 1000.')
    rules = DEFAULTS.copy()
    rules.update({k: data.get('rules', {}).get(k, v) for k, v in rules.items()})
    for k in ('candle_minutes', 'stochrsi_rsi_period', 'stochrsi_lookback', 'stochrsi_k_smooth', 'stochrsi_d_smooth'):
        rules[k] = whole_number(rules[k], k)
        if not 1 <= rules[k] <= 100:
            raise ValueError('Indicator lengths must be between 1 and 100.')
    if rules['candle_minutes'] not in (1, 3, 5, 10, 15, 30):
        raise ValueError('Select a supported candle interval.')
    for k in set(rules) - {'candle_minutes', 'stochrsi_rsi_period', 'stochrsi_lookback', 'stochrsi_k_smooth', 'stochrsi_d_smooth'}:
        rules[k] = float(rules[k])
        if not math.isfinite(rules[k]) or rules[k] < 0:
            raise ValueError('Rules must contain finite non-negative numbers.')
    if rules['entry_k'] > 100 or rules['exit_k_level'] > 100 or rules['stop_loss_points'] <= 0 or rules['entry_premium_min'] > rules['entry_premium_max']:
        raise ValueError('Invalid entry, exit, stop or premium band.')
    if automatic and not data.get('square_off', True):
        raise ValueError('Daily selection requires daily square-off so held contracts cannot be replaced overnight.')
    source = data.get('source', 'db')
    if source not in ('db', 'api'):
        raise ValueError('Choose Historify or broker history.')
    slip, cost = float(data.get('slippage', 1)), float(data.get('cost_per_order', 20))
    if not all(math.isfinite(v) and 0 <= v <= 10000 for v in (slip, cost)):
        raise ValueError('Invalid fill or cost assumptions.')
    return dict(start=str(start), end=str(end), contracts=selected, lots=lots, rules=rules,
                selection_mode='daily' if automatic else 'manual', sides=sides, source=source, slippage=slip, cost_per_order=cost, square_off=bool(data.get('square_off', True)))


def history(config, contract, key, start, end):
    cache = config.get('_history_cache')
    if cache is None:
        return get_history(contract['symbol'], contract['exchange'], '1m', start, end, api_key=key, source=config['source'])
    identifier = (contract['symbol'], contract['exchange'])
    if identifier not in cache:
        if config['source'] == 'api' and len(cache) >= 80:
            raise ValueError('Daily selection reached the 80-contract broker download limit. Shorten the dates or download data into Historify.')
        warm = (datetime.fromisoformat(config['_range_start']) - timedelta(days=30)).date().isoformat()
        cache[identifier] = get_history(contract['symbol'], contract['exchange'], '1m', warm, config['_range_end'], api_key=key, source=config['source'])
    ok, payload, status = cache[identifier]
    if not ok:
        return ok, payload, status
    low = datetime.fromisoformat(start).replace(tzinfo=IST).timestamp()
    high = (datetime.fromisoformat(end) + timedelta(days=1)).replace(tzinfo=IST).timestamp()
    rows = [row for row in payload['data'] if low <= float(row['timestamp']) < high]
    return True, {'data': rows}, 200


def run_daily(config, key):
    # Select using only quotes visible at that minute's close; never future-day prices.
    cache = {}
    config = {**config, '_history_cache': cache, '_range_start': config['start'], '_range_end': config['end']}
    underlying = config['contracts'][0]['underlying']
    spot_contract = {'symbol': underlying, 'exchange': 'BSE_INDEX' if underlying in ('SENSEX', 'BANKEX') else 'NSE_INDEX'}
    ok, payload, _ = history(config, spot_contract, key, config['start'], config['end'])
    if not ok:
        raise ValueError('Spot history is needed for daily ATM selection. Download it in Historify or choose broker history.')
    spots = {}
    for row in payload['data']:
        at = datetime.fromtimestamp(float(row['timestamp']), IST)
        if 555 <= at.hour * 60 + at.minute < 914 and math.isfinite(float(row['close'])) and float(row['close']) > 0:
            spots.setdefault(str(at.date()), []).append(row)
    if not spots:
        raise ValueError('No valid spot minutes in this date range.')
    # Keep broker work bounded; cached histories are reused by the actual replay.
    prices = {}
    failed = set()
    def quote(contract, timestamp):
        symbol = contract['symbol']
        if symbol not in prices and symbol not in failed:
            ok, data, _ = history(config, contract, key, config['start'], config['end'])
            if not ok:
                failed.add(symbol)
            else:
                prices[symbol] = {int(float(r['timestamp'])): float(r['close']) for r in data['data']
                    if math.isfinite(float(r['close'])) and float(r['close']) > 0
                    and all(math.isfinite(float(r[col])) for col in ('open', 'high', 'low', 'close'))
                    and 0 < float(r['low']) <= min(float(r['open']), float(r['close'])) <= max(float(r['open']), float(r['close'])) <= float(r['high'])}
        return prices.get(symbol, {}).get(int(float(timestamp)))
    selected_days, daily, trades, coverage, open_positions = [], [], [], [], []
    blocked_sides = set()
    for day, rows in sorted(spots.items()):
        selected = []
        for side in config['sides']:
            if side in blocked_sides:
                selected_days.append(dict(date=day, side=side, symbol=None, status='Skipped: unresolved earlier position'))
                continue
            candidates = [c for c in config['contracts'] if c['option_type'] == side]
            by_strike = {c['strike']: c for c in candidates}
            step = {'NIFTY': 50, 'BANKNIFTY': 100, 'FINNIFTY': 50, 'MIDCPNIFTY': 25, 'SENSEX': 100, 'BANKEX': 100}[underlying]
            chosen = None
            for row in sorted(rows, key=lambda r: r['timestamp']):
                atm = math.floor(float(row['close']) / step + .5) * step
                direction = 1 if side == 'CE' else -1
                order = [atm] + [atm + direction * n * step for n in range(1, 16)] + [atm - direction * n * step for n in range(1, 16)]
                for strike in order:
                    contract = by_strike.get(strike)
                    if not contract:
                        continue
                    premium = quote(contract, row['timestamp'])
                    if premium is not None and config['rules']['entry_premium_min'] <= premium <= config['rules']['entry_premium_max']:
                        chosen = contract
                        selected_at = datetime.fromtimestamp(float(row['timestamp']), IST) + timedelta(minutes=1)
                        selected_days.append(dict(date=day, side=side, symbol=contract['symbol'], strike=strike, premium=premium,
                            selected_at=selected_at.isoformat(), atm=atm, status='Selected'))
                        selected.append({**contract, '_selected_at': selected_at.isoformat()})
                        break
                if chosen:
                    break
            if not chosen:
                selected_days.append(dict(date=day, side=side, status='No fresh in-band quote within 15 strikes each way', symbol=None))
        if not selected:
            daily.append(dict(date=day, net=0, cumulative=0))
            continue
        replay = run({**config, 'selection_mode': 'manual', 'start': day, 'end': day, 'contracts': selected}, key)
        daily.extend(replay['daily']); trades.extend(replay['trades']); coverage.extend(replay['coverage']); open_positions.extend(replay['open_positions'])
        unresolved_symbols = {p['symbol'] for p in replay['open_positions']}
        blocked_sides.update(c['option_type'] for c in selected if c['symbol'] in unresolved_symbols)
    cumulative = 0
    for item in daily:
        cumulative += item['net']; item['cumulative'] = round(cumulative, 2)
    return dict(daily=daily, trades=trades, coverage=coverage, open_positions=open_positions, selections=selected_days,
        summary=dict(gross=round(sum(t['gross'] for t in trades), 2), costs=round(sum(t['costs'] for t in trades), 2),
                     net=round(sum(t['net'] for t in trades), 2), trades=len(trades), profitable_days=sum(d['net'] > 0 for d in daily)),
        assumptions=['Fixed expiry; select each side once per day from ATM, outward then inward, up to 15 levels.',
                     'Selection uses matching one-minute spot and option closes, known only at minute end. The contract stays fixed for that day.',
                     'Only spot-history dates are evaluated. Missing candidate histories can change which contract is selected.',
                     'No selection means no trades for that side. Prices and signals are never borrowed from later minutes.',
                     'Next-minute-open fills, slippage, minute-low stops, estimated order costs and 15:30 square-off are modeling assumptions. Missing held-trade minutes leave unresolved positions and suspend that side.'],
        config={k: v for k, v in config.items() if not k.startswith('_') and k != 'contracts'})


def run(config, key):
    import pandas as pd
    rules = config['rules']
    trades, coverage, unresolved = [], [], []
    days = set()
    for contract in config['contracts']:
        # Fetch extra history for indicator warm-up, without signals before start.
        warm = (datetime.fromisoformat(config['start']) - timedelta(days=30)).date().isoformat()
        ok, payload, _ = history(config, contract, key, warm, config['end'])
        if not ok:
            raise ValueError(payload.get('message', 'History unavailable'))
        df = pd.DataFrame(payload['data'])
        if df.empty:
            raise ValueError('No one-minute history for ' + contract['symbol'])
        ts = df['timestamp']
        if pd.api.types.is_numeric_dtype(ts):
            df['time'] = pd.to_datetime(ts, unit='s', utc=True).dt.tz_convert(IST)
        else:
            parsed = pd.to_datetime(ts)
            df['time'] = parsed.dt.tz_localize(IST) if parsed.dt.tz is None else parsed.dt.tz_convert(IST)
        df = df.sort_values('time').drop_duplicates('time')
        for col in ('open', 'high', 'low', 'close'):
            df[col] = pd.to_numeric(df[col], errors='coerce')
        valid = np.isfinite(df[['open', 'high', 'low', 'close']]).all(axis=1) & (df['low'] > 0) & (df['high'] >= df[['open', 'close', 'low']].max(axis=1)) & (df['low'] <= df[['open', 'close']].min(axis=1))
        invalid = int((~valid).sum())
        df = df[valid & (df['time'].dt.hour * 60 + df['time'].dt.minute >= 555) & (df['time'].dt.hour * 60 + df['time'].dt.minute < 930)]
        bars = list(df.to_dict('records'))
        usable = [b for b in bars if config['start'] <= str(b['time'].date()) <= config['end']]
        if not usable:
            raise ValueError('No valid session data in your dates for ' + contract['symbol'])
        available_days = sorted({str(b['time'].date()) for b in usable})
        days.update(available_days)
        missing_minutes = sum(max(0, int((b['time'] - a['time']).total_seconds() / 60) - 1) for a, b in zip(usable, usable[1:]) if a['time'].date() == b['time'].date())
        coverage.append(dict(symbol=contract['symbol'], bars=len(usable), dates=available_days, invalid_bars=invalid,
                             missing_minutes=missing_minutes, session_shortfall=max(0, 375 * len(available_days) - len(usable)), lot_size=contract['lot_size'], quantity=config['lots'] * contract['lot_size'],
                             lot_size_note='Current contract metadata / user override; verify historical lot size.'))
        indicator = IncrementalSRSI(rules['stochrsi_rsi_period'], rules['stochrsi_lookback'], rules['stochrsi_k_smooth'], rules['stochrsi_d_smooth'])
        bucket = previous_close = position = pending = None
        halted = False
        completed_closes = []
        qty = config['lots'] * contract['lot_size']
        def close(price, at, reason):
            nonlocal position
            gross = (price - position['price']) * qty
            costs = 2 * config['cost_per_order']
            trades.append(dict(symbol=contract['symbol'], quantity=qty, entry_time=position['time'], exit_time=at.isoformat(),
                entry=position['price'], exit=price, gross=gross, costs=costs, net=gross-costs, reason=reason, date=str(at.date())))
            position = None
        def censor(reason, at, last_close):
            nonlocal position, pending, halted
            if position:
                unresolved.append(dict(symbol=contract['symbol'], quantity=qty, entry=position['price'],
                    entry_time=position['time'], last_close=float(last_close), last_known_time=at.isoformat(), reason=reason))
                position = None
                halted = True
            pending = None
        for i, bar in enumerate(bars):
            at = bar['time'].to_pydatetime()
            day = str(at.date())
            active = config['start'] <= day <= config['end'] and (not contract.get('_selected_at') or at + timedelta(minutes=1) >= datetime.fromisoformat(contract['_selected_at']))
            minutes = at.hour * 60 + at.minute
            if i:
                prev_at = bars[i - 1]['time'].to_pydatetime()
                prev_minutes = prev_at.hour * 60 + prev_at.minute
                same_day = prev_at.date() == at.date()
                gap = (same_day and (at - prev_at).total_seconds() > 60) or (not same_day and (prev_minutes != 929 or minutes != 555))
                if gap:
                    if active and position:
                        censor('Missing session minutes: exit cannot be reconstructed', prev_at + timedelta(minutes=1), bars[i - 1]['close'])
                    pending = None
                    # A missing completed candle close invalidates subsequent RSI state.
                    missing_close = not same_day or any((minute - 555 + 1) % rules['candle_minutes'] == 0 for minute in range(prev_minutes + 1, minutes))
                    if missing_close:
                        indicator = IncrementalSRSI(rules['stochrsi_rsi_period'], rules['stochrsi_lookback'], rules['stochrsi_k_smooth'], rules['stochrsi_d_smooth'])
                        completed_closes = []
                        bucket = previous_close = None
            active = active and not halted
            if active and pending:
                if pending['side'] == 'sell' and position:
                    close(max(0, bar['open'] - config['slippage']), at, pending['reason'])
                elif pending['side'] == 'buy' and not position and at == pending['at'] and minutes < 915:
                    position = dict(price=bar['open'] + config['slippage'], time=at.isoformat())
                pending = None
            # A strict stop touch is handled before minute-close signals; gap fills at open.
            if active and position and bar['low'] < position['price'] - rules['stop_loss_points']:
                close(max(0, min(bar['open'], position['price'] - rules['stop_loss_points']) - config['slippage']), at, 'Stop touch (minute approximation)')
            current_bucket = (day, (minutes - 555) // rules['candle_minutes'])
            if bucket != current_bucket:
                if previous_close is not None:
                    indicator.append(previous_close)
                    completed_closes.append(previous_close)
                bucket = current_bucket
            previous_close = bar['close']
            k, _, spread = indicator.preview(previous_close)
            boundary = math.isfinite(k) and (abs(k - rules['entry_k']) <= 1e-9 or abs(k - rules['exit_k_level']) <= 1e-9)
            if boundary or (math.isfinite(spread) and spread <= 1e-9):
                ks, _ = ta.stochrsi(np.asarray(completed_closes + [float(previous_close)], dtype='float64'),
                    rsi_period=rules['stochrsi_rsi_period'], stoch_period=rules['stochrsi_lookback'],
                    k_period=rules['stochrsi_k_smooth'], d_period=rules['stochrsi_d_smooth'])
                k = float(ks[-1])
            if not active:
                continue
            signal_time = at + timedelta(minutes=1)
            next_same_day = i + 1 < len(bars) and bars[i + 1]['time'].date() == at.date()
            if config['square_off'] and (minutes == 929 or not next_same_day):
                if position:
                    if minutes == 929:
                        close(max(0, bar['close'] - config['slippage']), signal_time, '15:30 square-off assumption')
                    else:
                        censor('Missing 15:29 candle: square-off price is unknown', signal_time, bar['close'])
                pending = None
                continue
            if math.isfinite(k) and minutes < 929:
                if position and k >= rules['exit_k_level'] and bar['close'] >= position['price']:
                    pending = dict(side='sell', reason='K exit', at=signal_time)
                elif not position and k == rules['entry_k'] and signal_time.hour * 60 + signal_time.minute < 915 and rules['entry_premium_min'] <= bar['close'] <= rules['entry_premium_max']:
                    pending = dict(side='buy', at=signal_time)
        if position:
            unresolved.append(dict(symbol=contract['symbol'], quantity=qty, entry=position['price'], entry_time=position['time'], last_close=usable[-1]['close'], reason='Still open at end of requested range'))
    daily = [dict(date=day, net=round(sum(t['net'] for t in trades if t['date'] == day), 2)) for day in sorted(days)]
    cumulative = 0
    for d in daily:
        cumulative += d['net']; d['cumulative'] = round(cumulative, 2)
    return dict(daily=daily, trades=trades, coverage=coverage, open_positions=unresolved,
        summary=dict(gross=round(sum(t['gross'] for t in trades), 2), costs=round(sum(t['costs'] for t in trades), 2),
                     net=round(sum(t['net'] for t in trades), 2), trades=len(trades), profitable_days=sum(d['net'] > 0 for d in daily)),
        assumptions=['Fixed selected contracts and lots; no dynamic strike selection.', 'One-minute OHLCV approximates the running candle. Signals use minute closes; fills use next minute open plus/minus slippage.',
                     'Stop touches use minute low and adverse gap open; tick order and limit fills are unavailable.', 'Cost per order is a user estimate including brokerage and taxes, not a broker statement.',
                     'Only dates with data are shown; gaps and unresolved positions can materially change results.', 'Square-off requires the 15:29 candle. Truncated days or gaps during a held trade leave an unresolved position; no fill is invented.'], config={k: v for k, v in config.items() if not k.startswith('_')})


def worker(job_id, config, key):
    global ACTIVE
    try:
        result = (run_daily if config.get('selection_mode') == 'daily' else run)(config, key)
        with LOCK:
            JOBS[job_id].update(state='done', result=result)
    except Exception as exc:
        # Provider messages can contain credentials. Do not return raw exception traces.
        message = str(exc) if isinstance(exc, ValueError) else 'Backtest failed. Check history availability and server logs.'
        message = message.replace(key, '[redacted]')
        with LOCK:
            JOBS[job_id].update(state='error', message=message[:1000])
    finally:
        with LOCK:
            ACTIVE = False


@api.route('/<string:action>')
class Backtest(Resource):
    @limiter.limit(lambda: '120 per minute' if (request.view_args or {}).get('action') == 'status' else '10 per minute')
    def post(self, action):
        global ACTIVE
        data = request.get_json(silent=True) or {}
        key = data.get('apikey', '')
        owner = verify_api_key(key) if isinstance(key, str) and key else None
        if owner is None:
            return {'message': 'Invalid OpenAlgo API key'}, 403
        try:
            if action == 'catalog':
                return catalog()
            if action == 'status':
                with LOCK:
                    job = JOBS.get(data.get('job_id'))
                    job = job.copy() if job else None
                if not job or job['owner'] != str(owner):
                    return {'message': 'Backtest not found'}, 404
                return {k: v for k, v in job.items() if k != 'owner'}
            if action != 'run':
                return {'message': 'Unknown action'}, 404
            with LOCK:
                if ACTIVE:
                    return {'message': 'A backtest is already running. Try again shortly.'}, 409
                ACTIVE = True
            try:
                config = validate(data)
            except Exception:
                ACTIVE = False
                raise
            now = time.time()
            for identifier, old in list(JOBS.items()):
                if old['state'] != 'running' and now - old['created'] > 3600:
                    del JOBS[identifier]
            if len(JOBS) >= 5:
                oldest = min((k for k in JOBS if JOBS[k]['state'] != 'running'), key=lambda k: JOBS[k]['created'])
                del JOBS[oldest]
            job_id = uuid.uuid4().hex
            JOBS[job_id] = dict(owner=str(owner), state='running', created=now)
            Thread(target=worker, args=(job_id, config, key), name='srsi-backtest', daemon=True).start()
            return {'job_id': job_id, 'state': 'running'}, 202
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            return {'message': str(exc)[:300]}, 400
        except Exception:
            return {'message': 'Unable to load contract history catalog.'}, 503
