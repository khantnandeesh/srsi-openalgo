"""Fill intraday 6-OTM NIFTY option strikes for research-only replay."""
from __future__ import annotations
import json, os, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import duckdb
import pandas as pd
from openalgo import api
from services.historify_service import download_data
ROOT=Path('/home/ubuntu/openalgo'); RESULTS=ROOT/'strategies/research-recorder/ml_research/results'; STATUS=RESULTS/'intraday_6otm_backfill_status.json'; IST=ZoneInfo('Asia/Kolkata')
EXPIRIES=['06OCT26','13OCT26','19OCT26','27OCT26','03NOV26','23NOV26','29DEC26']

def key():
 for p in (ROOT/'strategies/config-ui/.env',ROOT/'.env'):
  if p.exists():
   for raw in p.read_text().splitlines():
    z=raw.strip()
    if z and not z.startswith('#') and '=' in z:
     k,v=z.split('=',1);os.environ.setdefault(k.strip(),v.strip().strip("\"'"))
 v=os.getenv('OPENALGO_API_KEY')
 if not v: raise RuntimeError('OPENALGO_API_KEY missing')
 return v

def save(x):
 x['updated_ist']=datetime.now(IST).isoformat(timespec='seconds'); STATUS.write_text(json.dumps(x,indent=2))

def main():
 RESULTS.mkdir(parents=True,exist_ok=True); api_key=key(); client=api(api_key=api_key,host='http://127.0.0.1:5000')
 con=duckdb.connect(str(ROOT/'db/historify.duckdb'),read_only=True);spot=con.execute("select timestamp,close from market_data where symbol='NIFTY'").fetchdf();con.close()
 spot['ts']=pd.to_datetime(spot.timestamp,unit='s',utc=True).dt.tz_convert(IST);spot=spot[(spot.ts>='2026-09-01')&(spot.ts<'2026-10-01')]
 atm=(spot.close/50).round().astype(int)*50; ce=set((atm+300).tolist()); pe=set((atm-300).tolist())
 contracts=[]
 for expiry in EXPIRIES:
  r=client.optionchain(underlying='NIFTY',exchange='NSE_INDEX',expiry_date=expiry)
  if not isinstance(r,dict) or r.get('status')!='success': continue
  for row in r.get('chain') or []:
   if not isinstance(row,dict):continue
   try:s=int(float(row.get('strike')))
   except (TypeError,ValueError):continue
   for leg_key,targets in (('ce',ce),('pe',pe)):
    leg=row.get(leg_key)
    if s in targets and isinstance(leg,dict) and leg.get('symbol'): contracts.append(str(leg['symbol']))
 contracts=list(dict.fromkeys(contracts)); state={'kind':'intraday_6otm_historify_backfill','contract_count':len(contracts),'completed':0,'successes':0,'failures':0,'records':0,'errors':[]};save(state);print(f'START {len(contracts)} contracts',flush=True)
 for n,symbol in enumerate(contracts,1):
  try:
   ok,r,code=download_data(symbol=symbol,exchange='NFO',interval='1m',start_date='2026-08-24',end_date='2026-09-30',api_key=api_key);rows=int(r.get('records',0) or 0) if isinstance(r,dict) else 0;state['records']+=rows
   if ok:state['successes']+=1;print(f'{n}/{len(contracts)} OK {symbol} rows={rows}',flush=True)
   else:state['failures']+=1;state['errors'].append({'symbol':symbol,'code':code,'message':str(r)[:160]});print(f'{n}/{len(contracts)} FAIL {symbol} {code}',flush=True)
  except Exception as ex:state['failures']+=1;state['errors'].append({'symbol':symbol,'message':str(ex)[:160]});print(f'{n}/{len(contracts)} ERROR {symbol} {ex}',flush=True)
  state['completed']=n;save(state)
  if n<len(contracts):time.sleep(.65)
 state['finished']=True;save(state);print(json.dumps(state),flush=True)
if __name__=='__main__':main()
