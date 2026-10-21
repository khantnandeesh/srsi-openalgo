#!/usr/bin/env python3
"""Research-only October1 actual one-second quote replay. No orders or settings."""
from pathlib import Path
import json,math,re,sys
import numpy as np,pandas as pd
sys.path.insert(0,str(Path(__file__).parent))
import run_srsi_ml_research as C

BASE=Path(__file__).parent;OUT=BASE/'results/1s_research';OUT.mkdir(parents=True,exist_ok=True)
DAY=pd.Timestamp('2026-10-01');QTY=325;PREMIUM_MIN=80.;PREMIUM_MAX=150.;ENTRY_SLIP=.25;EXIT_SLIP=1.
POLICIES=['exact_6otm','premium_80_150_near115_0to6otm']
METHOD=dict(day=str(DAY.date()),policies=POLICIES,qty=QTY,lots=5,candle_minutes=5,
 srsi='RSI4, Stoch4, K2,D2; selected contract must actually transition from finite nonzero K to zero on this recorded second',
 spot='last completed NIFTY1m close; close only available at timestamp+1minute; no future minute closes or live spot tape',
 expiry='nearest recorded active expiry among fresh qualifying contracts',premium_policy='0..6 OTM, inclusive80..150, closest115; then zero-transition',
 quotes='recorded1s close available at source timestamp+1second; only fresh<=5seconds qualify for newentry',
 warmup='full five-minute positive-close buckets from preOct1cache plus currentday1m history known before first recorded quote; minimum10completedbars',
 post_capture_history='only observed close at candle boundary, never later backfilled with future data; missed whole candle invalidates SRSI until restart',
 entry_cutoff='13:30 IST',exit='K>=80 and currentprice>=actualmodeledbuyprice; strict currentprice<entry-15; EOD15:25 iffresh',
 execution='next recorded quote after signal, buy+0.25, sell-1point; if no nextquote within5seconds retainunresolved rather than inferfill',
 missing='held gap>5seconds flagged; missingtailunresolved; closedtrade totals are resolvedsubtotals only',
 fee='40brokerage roundtrip; transaction0.03553%,SEBI0.0001%,stamp0.003%buy,STT0.15%sell,GST18%brokerage+transaction+SEBI',
 limitations='one day, quoteaggregates not guaranteed completeexchange ticks, no bidask or five-second limitfill proof; policies predeclared not tuned on results')
(OUT/'method.json').write_text(json.dumps(METHOD,indent=2))
q=pd.read_csv(BASE.parent/'data/nifty_1s_20261001.csv.gz')
q['source_time']=pd.to_datetime(q.timestamp_ist).dt.tz_localize(None)
q=q[(q.source_time>=DAY+pd.Timedelta(hours=9,minutes=15))&(q.source_time<DAY+pd.Timedelta(hours=15,minutes=30))].copy()
q=q[q.close.gt(0)&np.isfinite(q.close)].drop_duplicates(['symbol','source_time']).sort_values(['symbol','source_time'])
meta=q.symbol.map(C.parse_symbol)
q['expiry']=[pd.Timestamp(x[0]) for x in meta];q['strike']=[x[1] for x in meta];q['side']=[x[2] for x in meta]
cache=pd.read_parquet(BASE/'results/expiry_locked_6otm_5lots_bars.parquet',columns=['symbol','timestamp','close'])
cache['timestamp']=pd.to_datetime(cache.timestamp)
cache=cache[(cache.timestamp<DAY)&cache.symbol.isin(q.symbol.unique())]
rawdir=BASE/'results/prospective_20261001'
spot=pd.read_csv(rawdir/'NIFTY.csv');spot['timestamp']=pd.to_datetime(spot.timestamp)
spot['available_time']=spot.timestamp+pd.Timedelta(minutes=1)
spot=spot[['available_time','close']].rename(columns={'close':'spot'}).drop_duplicates('available_time').sort_values('available_time')
def bucket_for(t):
 start=t.normalize()+pd.Timedelta(hours=9,minutes=15)
 return start+pd.Timedelta(minutes=int((t-start).total_seconds()//300)*5)
def closed_bars(m,first):
 if m.empty:return []
 m=m[m.timestamp+pd.Timedelta(minutes=1)<=first].copy()
 m=m[m.close.gt(0)&np.isfinite(m.close)].sort_values('timestamp').drop_duplicates('timestamp')
 if m.empty:return []
 m['bucket']=m.timestamp.map(bucket_for)
 a=m.groupby('bucket').agg(n=('timestamp','size'),first=('timestamp','min'),last=('timestamp','max'),price=('close','last'))
 a=a[(a.n==5)&(a['first']==a.index)&(a['last']==a.index+pd.Timedelta(minutes=4))]
 return a.price.tolist()
parts=[];coverage=[]
print('Computing causal1s indicators',len(q),q.symbol.nunique(),flush=True)
for symbol,g in q.groupby('symbol',sort=True):
 g=g.sort_values('source_time').copy();first=g.source_time.iloc[0]+pd.Timedelta(seconds=1)
 past=cache[cache.symbol.eq(symbol)][['timestamp','close']]
 rawpath=rawdir/(symbol+'.csv')
 if rawpath.exists():
  raw=pd.read_csv(rawpath);raw['timestamp']=pd.to_datetime(raw.timestamp)
  past=pd.concat([past,raw[['timestamp','close']]],ignore_index=True)
 initial=closed_bars(past,first)
 state=C.StochState()
 for price in initial:state.finalize(price)
 completed_count=len(initial);last_bucket=None;last_close=None;last_source=None;indicator_valid=True
 ks=[];ds=[];edges=[];valids=[];previous_k=np.nan;gaps=[];missed_buckets=0
 for r in g.itertuples():
  current_bucket=bucket_for(r.source_time)
  gap=(r.source_time-last_source).total_seconds() if last_source is not None else 0.
  if last_bucket is not None and current_bucket!=last_bucket:
   bucket_distance=int((current_bucket-last_bucket).total_seconds()/300)
   if bucket_distance==1 and last_source>=last_bucket+pd.Timedelta(minutes=5)-pd.Timedelta(seconds=5):
    state.finalize(last_close);completed_count+=1
   else:
    indicator_valid=False;missed_buckets+=max(1,bucket_distance)
  if indicator_valid and completed_count>=10:
   k,d=state.provisional(float(r.close))
  else:k=d=np.nan
  edge=bool(np.isfinite(k) and np.isfinite(previous_k) and abs(k)<1e-10 and abs(previous_k)>=1e-10 and gap<=5.)
  ks.append(k);ds.append(d);edges.append(edge);valids.append(indicator_valid and completed_count>=10);gaps.append(gap)
  previous_k=k;last_bucket=current_bucket;last_close=float(r.close);last_source=r.source_time
 g['k']=ks;g['d']=ds;g['zero_transition']=edges;g['indicator_valid']=valids;g['source_gap_seconds']=gaps
 g['available_time']=g.source_time+pd.Timedelta(seconds=1)
 coverage.append(dict(symbol=symbol,side=g.side.iloc[0],expiry=str(g.expiry.iloc[0].date()),rows=len(g),
  first_quote=g.source_time.iloc[0],last_quote=g.source_time.iloc[-1],warmup_closed5m=len(initial),valid_indicator_rows=int(g.indicator_valid.sum()),
  source_gaps_gt5=int((g.source_gap_seconds>5).sum()),largest_source_gap_seconds=float(g.source_gap_seconds.max()),missed_whole_candles=missed_buckets))
 parts.append(g[['available_time','source_time','symbol','expiry','strike','side','close','k','d','zero_transition','indicator_valid','source_gap_seconds']])
events=pd.concat(parts,ignore_index=True).sort_values(['available_time','symbol'])
events=pd.merge_asof(events,spot,on='available_time',direction='backward')
events.to_parquet(OUT/'causal_1s_features.parquet',index=False)
pd.DataFrame(coverage).to_csv(OUT/'coverage.csv',index=False)
print('Indicatorfeatures saved',flush=True)

def charges(entry,exit):
 buy=entry*QTY;sell=exit*QTY;turn=buy+sell
 b=40.;txn=turn*.0003553;sebi=turn*.000001
 return b+txn+sebi+buy*.00003+sell*.0015+.18*(b+txn+sebi)
def flat_state():return dict(phase='FLAT',symbol=None,record=None,pending_time=None,last_source=None,gaps=0,maxgap=0.,seconds_unobserved=0.)
states={p:{s:flat_state() for s in ['CE','PE']} for p in POLICIES}
latest={};records=[];signals=[];seq=0
def finish(st,status,reason):
 r=st['record'];r.update(status=status,reason=reason,held_gaps_gt5=st['gaps'],largest_held_gap_seconds=st['maxgap'],
   held_unobserved_seconds=st['seconds_unobserved'],last_observed_source=st['last_source'])
 records.append(r);st['phase']='DONE' if status.startswith('UNRESOLVED') else 'FLAT';st['symbol']=None;st['record']=None
def selected(policy,side,t,spot_value):
 if not np.isfinite(spot_value):return None
 atm=int(math.floor(float(spot_value)/50.+.5)*50)
 cand=[]
 for symbol,r in latest.items():
  if r.side!=side or r.expiry<DAY or (t-r.available_time).total_seconds()>5 or not r.indicator_valid or not np.isfinite(r.k):continue
  distance=(r.strike-atm)/50. if side=='CE' else (atm-r.strike)/50.
  if policy=='exact_6otm':
   if distance!=6:continue
  else:
   if not(0<=distance<=6 and PREMIUM_MIN<=r.close<=PREMIUM_MAX):continue
  cand.append(r)
 if not cand:return None
 earliest=min(r.expiry for r in cand);cand=[r for r in cand if r.expiry==earliest]
 if policy=='exact_6otm':return sorted(cand,key=lambda r:r.symbol)[0]
 return sorted(cand,key=lambda r:(abs(float(r.close)-115.),r.strike,r.symbol))[0]

for t,g in events.groupby('available_time',sort=True):
 if t>DAY+pd.Timedelta(hours=15,minutes=25,seconds=8):break
 current={}
 for row in g.itertuples(index=False):latest[row.symbol]=row;current[row.symbol]=row
 spot_value=float(g.spot.iloc[0]) if len(g) else np.nan
 for policy in POLICIES:
  for side in ['CE','PE']:
   st=states[policy][side]
   if st['phase']=='DONE':continue
   row=current.get(st['symbol']) if st['symbol'] else None
   if st['phase'] in {'OPEN','SELL_PENDING'} and row is not None:
    if st['last_source'] is not None:
     gap=(row.source_time-st['last_source']).total_seconds()
     if gap>5:st['gaps']+=1;st['maxgap']=max(st['maxgap'],gap);st['seconds_unobserved']+=max(0.,gap-1.)
    st['last_source']=row.source_time
   if st['phase'] in {'BUY_PENDING','SELL_PENDING'}:
    elapsed=(t-st['pending_time']).total_seconds()
    if row is not None and t>st['pending_time'] and elapsed<=5:
     r=st['record']
     if st['phase']=='BUY_PENDING':
      entry=float(row.close)+ENTRY_SLIP;r.update(entry_time=t,entry_source=row.source_time,raw_entry=float(row.close),entry_price=entry)
      st['phase']='OPEN';st['last_source']=row.source_time
     else:
      sell=max(0.,float(row.close)-EXIT_SLIP);r.update(exit_time=t,exit_source=row.source_time,raw_exit=float(row.close),exit_price=sell,
        raw_gross=(float(row.close)-r['raw_entry'])*QTY,gross_after_slippage=(sell-r['entry_price'])*QTY)
      r['fees']=charges(r['entry_price'],sell);r['net']=r['gross_after_slippage']-r['fees']
      reason=r['exit_reason'];finish(st,'RESOLVED',reason)
     continue
    if elapsed>5:
     finish(st,'UNRESOLVED_ENTRY' if st['phase']=='BUY_PENDING' else 'UNRESOLVED_EXIT','NO_NEXT_QUOTE_WITHIN5_SECONDS')
    continue
   if st['phase']=='OPEN':
    r=st['record'];reason=None
    if row is not None:
     price=float(row.close)
     if price<r['entry_price']-15:reason='STOP_LOSS_BELOW15_POINTS'
     elif np.isfinite(row.k) and row.k>=80 and price>=r['entry_price']:reason='PROFITABLE_SRSI_GE80'
    if reason is None and t>=DAY+pd.Timedelta(hours=15,minutes=25):
     held=latest.get(st['symbol'])
     if held is None or (t-held.available_time).total_seconds()>5:
      finish(st,'UNRESOLVED_EXIT','NO_FRESH_EOD_QUOTE');continue
     reason='EOD15_25'
    if reason:
     r.update(exit_signal_time=t,exit_reason=reason,exit_signal_price=float(latest[st['symbol']].close),exit_signal_k=float(latest[st['symbol']].k))
     st['phase']='SELL_PENDING';st['pending_time']=t
    continue
   if st['phase']=='FLAT' and t<DAY+pd.Timedelta(hours=13,minutes=30) and t>=DAY+pd.Timedelta(hours=9,minutes=16):
    candidate=selected(policy,side,t,spot_value)
    if candidate is None or candidate.symbol not in current or not candidate.zero_transition:continue
    seq+=1;r=dict(policy=policy,trade_id=seq,side=side,symbol=candidate.symbol,expiry=str(candidate.expiry.date()),
      strike=candidate.strike,signal_time=t,signal_source=candidate.source_time,signal_price=float(candidate.close),signal_k=float(candidate.k),
      spot_at_signal=spot_value,qty=QTY,held_gaps_gt5=0,largest_held_gap_seconds=0.,held_unobserved_seconds=0.)
    signals.append(r.copy());st.update(phase='BUY_PENDING',symbol=candidate.symbol,record=r,pending_time=t,last_source=None,gaps=0,maxgap=0.,seconds_unobserved=0.)
for policy in POLICIES:
 for side,st in states[policy].items():
  if st['phase'] not in ['FLAT','DONE']:finish(st,'UNRESOLVED_ENTRY' if st['phase']=='BUY_PENDING' else 'UNRESOLVED_EXIT','DATA_ENDED_WITH_POSITION_OR_ORDER_OPEN')
tr=pd.DataFrame(records);sg=pd.DataFrame(signals)
tr.to_csv(OUT/'trades.csv',index=False);sg.to_csv(OUT/'signals.csv',index=False)
summary=[]
for p in POLICIES:
 a=tr[tr.policy.eq(p)] if len(tr) else pd.DataFrame();resolved=a[a.status.eq('RESOLVED')] if len(a) else pd.DataFrame()
 row=dict(policy=p,signals=int(len(a)),resolved=int(len(resolved)),unresolved=int(a.status.str.startswith('UNRESOLVED').sum()) if len(a) else 0,
  raw_gross=float(resolved.raw_gross.sum()) if len(resolved) else 0.,gross_after_slippage=float(resolved.gross_after_slippage.sum()) if len(resolved) else 0.,
  fees=float(resolved.fees.sum()) if len(resolved) else 0.,net_resolved_subtotal=float(resolved.net.sum()) if len(resolved) else 0.,
  resolved_held_gaps_gt5=int((resolved.held_gaps_gt5>0).sum()) if len(resolved) else 0)
 summary.append(row)
su=pd.DataFrame(summary);su.to_csv(OUT/'summary.csv',index=False)
print(su.to_string(index=False),flush=True)
if len(tr):print(tr.to_string(index=False),flush=True)
print('Saved',OUT,flush=True)

