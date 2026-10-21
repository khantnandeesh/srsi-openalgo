#!/usr/bin/env python3
"""Research only: two predeclared 100-point NIFTY credit-spread variants."""
from pathlib import Path
import json, sys
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent))
import run_srsi_ml_research as C

BASE=Path(__file__).parent
OUT=BASE/'results/credit_spread_research'
OUT.mkdir(parents=True,exist_ok=True)
EXPIRY='29SEP26'; QTY=325; WIDTH=100.
START='2026-09-01'; END='2026-09-24'
VARIANTS=['ema20_trend','ema20_trend_srsi_reversal']
METHOD={
'expiry':EXPIRY,'quantity':QTY,'lots':5,'width_points':WIDTH,
'short_strike':'ATM+300 CE bearish trend; ATM-300 PE bullish trend; hedge 100 further OTM',
'trend':'NIFTY latest fully observed completed 5m close vs EMA20 and EMA20 slope; both agree',
'second_variant':'trend plus current short-option 5m provisional K<80 and preceding minute K>=80',
'entry_window':'10:00 inclusive to 13:00 exclusive IST',
'entry':'next minute paired opens after signal; missing paired open retained unresolved with day halted',
'prior_liquidity':'both legs >=9 positive-volume observed bars in preceding 10 minutes inclusive latest completed minute',
'exit':'synchronized minute close at 50% initial-credit profit or 100% credit loss, max 30 elapsed minutes',
'spread_extrema':'not constructed; only synchronized observed closes',
'slippage':'1 point adverse per leg on entry and exit =4 points per spread round trip',
'fees':'brokerage20/order x4, transaction0.03553%, SEBI0.0001%, stamp0.003%buy, STT0.15%sales, GST18%brokerage+transaction+SEBI',
'selection':'two predeclared variants; train Sep1-11, validation Sep15-18; winner frozen before test Sep21-24',
'limitation':'retrospective dates previously seen in other research, not pristine untouched holdout',
'no_future_drop':'entry chosen without future path checks; missing paths retained unresolved',
'position_limit':'one spread open, max2/day, fixed5lots, no averaging'}
(OUT/'method.json').write_text(json.dumps(METHOD,indent=2))
print('Loading original archived CSVs and spot',flush=True)
options,spot=C.load_all()
options=options[options.symbol.str.contains('29SEP26')].copy()
options=options.drop(columns=['spot'],errors='ignore')
spot=spot.drop_duplicates('timestamp').sort_values('timestamp')
options=options.merge(spot,on='timestamp',how='left')
print('Archived rows',len(options),'symbols',options.symbol.nunique(),flush=True)
spot=spot.copy()
spot['day']=spot.timestamp.dt.date.astype(str)
spot['bucket']=spot.timestamp.dt.normalize()+pd.Timedelta(hours=9,minutes=15)+pd.to_timedelta(
  ((spot.timestamp.dt.hour*60+spot.timestamp.dt.minute-555)//5)*5,unit='m')
completed=spot.groupby(['day','bucket']).agg(count=('timestamp','size'),first=('timestamp','min'),last=('timestamp','max'),close=('spot','last')).reset_index()
completed=completed[(completed['count']==5)&(completed['first']==completed.bucket)&(completed['last']==completed.bucket+pd.Timedelta(minutes=4))].copy()
completed['effective_time']=completed.bucket+pd.Timedelta(minutes=5)
completed=completed.sort_values('effective_time')
completed['ema']=completed.close.ewm(span=20,adjust=False,min_periods=20).mean()
completed['ema_previous']=completed.ema.shift()
trend_rows=spot.copy()
trend_rows['effective_time']=trend_rows.timestamp+pd.Timedelta(minutes=1)
trend_rows=pd.merge_asof(trend_rows.sort_values('effective_time'),completed[['effective_time','ema','ema_previous']],on='effective_time',direction='backward')
trend_rows['side']=np.where((trend_rows.spot>trend_rows.ema)&(trend_rows.ema>trend_rows.ema_previous),'PE',
  np.where((trend_rows.spot<trend_rows.ema)&(trend_rows.ema<trend_rows.ema_previous),'CE',''))
trend_rows['atm']=(np.floor(trend_rows.spot/50.+.5)*50).astype(int)
symbol_data={}
for symbol,g in options.groupby('symbol',sort=True):
    g=C.interval_kd(g.sort_values('sample_time'),5)
    g['prev_k']=g.k.shift()
    g=g.set_index('timestamp').sort_index()
    active=(g.volume.fillna(0)>0).astype(int)
    g['density']=active.rolling('10min',closed='right').sum()
    symbol_data[symbol]=g
print('Indicators computed',len(symbol_data),flush=True)
all_days=sorted(x for x in trend_rows.day.unique() if START<=x<=END)
spot_by_day={d:g.sort_values('timestamp') for d,g in trend_rows.groupby('day') if d in all_days}
def fees(se,le,sx,lx):
    sales=(se+lx)*QTY;buys=(le+sx)*QTY;turn=sales+buys
    brokerage=80.;transaction=turn*.0003553;sebi=turn*.000001
    return brokerage+transaction+sebi+buys*.00003+sales*.0015+.18*(brokerage+transaction+sebi)
def phase(day):
    if day<='2026-09-11':return 'train'
    if '2026-09-15'<=day<='2026-09-18':return 'validation'
    if '2026-09-21'<=day<='2026-09-24':return 'test'
    return 'excluded'
def simulate(variant,days):
    records=[];daily=[]
    for day in days:
        day_records=[];busy_until=None;count=0
        for s in spot_by_day.get(day,pd.DataFrame()).itertuples():
            t=s.timestamp;effective=t+pd.Timedelta(minutes=1)
            if not(600<=effective.hour*60+effective.minute<780):continue
            if count>=2 or (busy_until is not None and effective<=busy_until):continue
            if not s.side:continue
            strike=int(s.atm)+(300 if s.side=='CE' else -300)
            hedge=strike+(100 if s.side=='CE' else -100)
            ss=f'NIFTY{EXPIRY}{strike}{s.side}';ls=f'NIFTY{EXPIRY}{hedge}{s.side}'
            short=symbol_data.get(ss);long=symbol_data.get(ls)
            if short is None or long is None or t not in short.index or t not in long.index:continue
            a=short.loc[t];b=long.loc[t]
            if a.density<9 or b.density<9 or a.volume<=0 or b.volume<=0:continue
            credit=float(a.close-b.close)
            if not(2<credit<WIDTH):continue
            if variant=='ema20_trend_srsi_reversal' and not(np.isfinite(a.k) and np.isfinite(a.prev_k) and a.k<80<=a.prev_k):continue
            r=dict(variant=variant,phase=phase(day),day=day,signal_time=effective,short_symbol=ss,long_symbol=ls,
              short_strike=strike,long_strike=hedge,side=s.side,spot=s.spot,k=a.k,previous_k=a.prev_k,
              credit_at_signal=credit,density_short=a.density,density_long=b.density)
            entry_time=effective;count+=1
            if entry_time not in short.index or entry_time not in long.index:
                r.update(status='UNRESOLVED_ENTRY',reason='MISSING_PAIRED_NEXT_OPEN',entry_time=entry_time,missing_joint_minutes=np.nan)
                day_records.append(r);busy_until=pd.Timestamp(day)+pd.Timedelta(hours=15,minutes=30);break
            sr=short.loc[entry_time];lr=long.loc[entry_time]
            se=float(sr.open)-1.;le=float(lr.open)+1.
            mid_credit=float(sr.open-lr.open);entry_credit=se-le
            r.update(entry_time=entry_time,short_entry=se,long_entry=le,raw_credit=mid_credit,entry_credit=entry_credit)
            if not(0<entry_credit<WIDTH) or se<=0:
                r.update(status='NOT_FILLED_PROXY',reason='INVALID_NEXT_OPEN_CREDIT',missing_joint_minutes=0)
                day_records.append(r);busy_until=entry_time;continue
            path=short[['close']].rename(columns={'close':'short_close'}).join(long[['close']].rename(columns={'close':'long_close'}),how='inner')
            max_time=entry_time+pd.Timedelta(minutes=30)
            path=path[(path.index>=entry_time)&(path.index+pd.Timedelta(minutes=1)<=max_time)]
            exit_row=None;reason=None
            for tick,p in path.iterrows():
                mid_debit=float(p.short_close-p.long_close)
                if mid_debit<=mid_credit*.5:exit_row=(tick,p);reason='TAKE_PROFIT_50_PERCENT';break
                if mid_debit>=mid_credit*2:exit_row=(tick,p);reason='STOP_LOSS_100_PERCENT';break
                if tick+pd.Timedelta(minutes=1)>=max_time:exit_row=(tick,p);reason='MAX_HOLD_30_MINUTES';break
            if exit_row is None:
                r.update(status='UNRESOLVED_EXIT',reason='MISSING_JOINT_EXIT_DATA',missing_joint_minutes=30-len(path),
                  last_observed_time=(path.index[-1]+pd.Timedelta(minutes=1)) if len(path) else None)
                day_records.append(r);busy_until=pd.Timestamp(day)+pd.Timedelta(hours=15,minutes=30);break
            tick,p=exit_row;exit_time=tick+pd.Timedelta(minutes=1)
            sx=float(p.short_close)+1.;lx=max(0.,float(p.long_close)-1.)
            raw_points=mid_credit-float(p.short_close-p.long_close)
            slipped_points=entry_credit-(sx-lx);charged=fees(se,le,sx,lx)
            held=int((exit_time-entry_time).total_seconds()/60)
            actual=int(((path.index>=entry_time)&(path.index<=tick)).sum())
            r.update(status='RESOLVED',reason=reason,exit_time=exit_time,short_exit=sx,long_exit=lx,held_minutes=held,
              missing_joint_minutes=held-actual,raw_points=raw_points,raw_gross=raw_points*QTY,
              gross_after_slippage=slipped_points*QTY,fees=charged,net=slipped_points*QTY-charged)
            day_records.append(r);busy_until=exit_time
        records.extend(day_records)
        resolved=[r for r in day_records if r.get('status')=='RESOLVED']
        unresolved=[r for r in day_records if r.get('status','').startswith('UNRESOLVED')]
        daily.append(dict(variant=variant,day=day,phase=phase(day),trades=len(resolved),unresolved=len(unresolved),
            raw_gross=sum(r['raw_gross'] for r in resolved),gross_after_slippage=sum(r['gross_after_slippage'] for r in resolved),
            fees=sum(r['fees'] for r in resolved),net=sum(r['net'] for r in resolved),complete=not unresolved))
    return pd.DataFrame(records),pd.DataFrame(daily)
def summarize(v,p,tr,da):
    sel=da[da.phase==p];resolved=tr[tr.phase.eq(p)&tr.status.eq('RESOLVED')] if len(tr) else pd.DataFrame()
    return dict(variant=v,phase=p,days=len(sel),trades=int(sel.trades.sum()),unresolved=int(sel.unresolved.sum()),
        raw_gross=float(sel.raw_gross.sum()),gross_after_slippage=float(sel.gross_after_slippage.sum()),fees=float(sel.fees.sum()),net=float(sel.net.sum()),
        missing_path_trades=int((resolved.missing_joint_minutes>0).sum()) if len(resolved) else 0)
selection_days=[d for d in all_days if phase(d) in ['train','validation']]
summ=[]
for v in VARIANTS:
    tr,da=simulate(v,selection_days)
    tr.to_csv(OUT/f'{v}_development_trades.csv',index=False);da.to_csv(OUT/f'{v}_development_daily.csv',index=False)
    for p in ['train','validation']:summ.append(summarize(v,p,tr,da))
val=[r for r in summ if r['phase']=='validation']
rank=sorted(val,key=lambda r:(r['unresolved']==0,r['net']),reverse=True)
winner=rank[0]['variant'];eligible=rank[0]['unresolved']==0 and rank[0]['net']>0 and rank[0]['trades']>=3
selection={'selected_variant':winner,'positive_validation_eligible':eligible,'validation_results':val,'frozen_before_test':True}
(OUT/'selection_before_test.json').write_text(json.dumps(selection,indent=2))
print('Selection frozen',selection,flush=True)
test_days=[d for d in all_days if phase(d)=='test']
tr,da=simulate(winner,test_days)
tr.to_csv(OUT/'selected_test_trades.csv',index=False);da.to_csv(OUT/'selected_test_daily.csv',index=False)
summ.append(summarize(winner,'test',tr,da))
summary=pd.DataFrame(summ);summary.to_csv(OUT/'summary.csv',index=False)
print(summary.to_string(index=False),flush=True)
print('Test daily',da.to_string(index=False),flush=True)
print('Saved',OUT,flush=True)

