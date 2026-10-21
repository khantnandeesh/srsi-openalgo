"""Backfill 1m bars for valid active NIFTY expiries used in offline research only."""
from __future__ import annotations
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import duckdb
import pandas as pd
from openalgo import api
from services.historify_service import download_data

ROOT = Path('/home/ubuntu/openalgo')
RESEARCH = ROOT / 'strategies/research-recorder/ml_research'
RESULTS = RESEARCH / 'results'
STATUS = RESULTS / 'active_expiry_backfill_status.json'
IST = ZoneInfo('Asia/Kolkata')
EXPIRIES = ['06OCT26','13OCT26','19OCT26','27OCT26','03NOV26','23NOV26','29DEC26']
START_DATE = '2026-08-24'
END_DATE = '2026-09-30'


def load_key() -> str:
    for path in (ROOT / 'strategies/config-ui/.env', ROOT / '.env'):
        if not path.exists():
            continue
        for raw in path.read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    value = os.getenv('OPENALGO_API_KEY')
    if not value:
        raise RuntimeError('OPENALGO_API_KEY missing')
    return value


def write_status(payload: dict) -> None:
    payload['updated_ist'] = datetime.now(IST).isoformat(timespec='seconds')
    STATUS.write_text(json.dumps(payload, indent=2), encoding='utf-8')


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    api_key = load_key()
    client = api(api_key=api_key, host='http://127.0.0.1:5000')
    con = duckdb.connect(str(ROOT / 'db/historify.duckdb'), read_only=True)
    spot = con.execute("select timestamp, close from market_data where symbol = 'NIFTY'").fetchdf()
    con.close()
    spot['timestamp'] = pd.to_datetime(spot['timestamp'], unit='s', utc=True).dt.tz_convert(IST)
    spot['date'] = spot['timestamp'].dt.date
    first = spot.sort_values('timestamp').groupby('date')['close'].first()
    ce_targets, pe_targets = set(), set()
    for value in first:
        atm = int(round(float(value) / 50.0) * 50)
        ce_targets.add(atm + 300)
        pe_targets.add(atm - 300)

    contracts = []
    skipped = []
    for expiry in EXPIRIES:
        response = client.optionchain(underlying='NIFTY', exchange='NSE_INDEX', expiry_date=expiry)
        if not isinstance(response, dict) or response.get('status') != 'success':
            skipped.append({'expiry': expiry, 'reason': str(response)[:250]})
            continue
        for item in response.get('chain') or []:
            if not isinstance(item, dict):
                continue
            try:
                strike = int(float(item.get('strike')))
            except (TypeError, ValueError):
                continue
            for side, targets in (('ce', ce_targets), ('pe', pe_targets)):
                leg = item.get(side)
                if strike in targets and isinstance(leg, dict) and leg.get('symbol'):
                    contracts.append({'expiry': expiry, 'side': side.upper(), 'strike': strike, 'symbol': str(leg['symbol']), 'exchange': 'NFO'})
    contracts = list({item['symbol']: item for item in contracts}.values())
    state = {'kind': 'active_expiry_historify_backfill', 'start_date': START_DATE, 'end_date': END_DATE, 'expiries': EXPIRIES, 'contract_count': len(contracts), 'completed': 0, 'successes': 0, 'failures': 0, 'records': 0, 'skipped_expiries': skipped, 'errors': []}
    write_status(state)
    print(f'START contracts={len(contracts)} expiries={len(EXPIRIES)}', flush=True)
    for number, contract in enumerate(contracts, start=1):
        try:
            ok, result, status_code = download_data(symbol=contract['symbol'], exchange='NFO', interval='1m', start_date=START_DATE, end_date=END_DATE, api_key=api_key)
            records = int(result.get('records', 0) or 0) if isinstance(result, dict) else 0
            state['records'] += records
            if ok:
                state['successes'] += 1
                print(f'{number}/{len(contracts)} OK {contract["symbol"]} rows={records}', flush=True)
            else:
                state['failures'] += 1
                msg = str(result.get('message', result) if isinstance(result, dict) else result)[:180]
                state['errors'].append({'symbol': contract['symbol'], 'status_code': status_code, 'message': msg})
                print(f'{number}/{len(contracts)} FAIL {contract["symbol"]} {status_code} {msg}', flush=True)
        except Exception as exc:
            state['failures'] += 1
            state['errors'].append({'symbol': contract['symbol'], 'message': f'{type(exc).__name__}: {str(exc)[:180]}'})
            print(f'{number}/{len(contracts)} ERROR {contract["symbol"]} {type(exc).__name__}: {str(exc)[:180]}', flush=True)
        state['completed'] = number
        write_status(state)
        if number < len(contracts):
            time.sleep(0.7)
    state['finished'] = True
    write_status(state)
    print(json.dumps(state, indent=2), flush=True)

if __name__ == '__main__':
    main()
