#!/usr/bin/env python3
"""Summarize historical SRSI event outcomes by signal premium and moneyness."""
from pathlib import Path
import duckdb
import numpy as np
import pandas as pd
import sys
sys.path.insert(0,'/home/ubuntu/openalgo/strategies/research-recorder/ml_research')
import run_srsi_ml_research as core

ROOT=Path('/home/ubuntu/openalgo/strategies/research-recorder/ml_research')
OUT=ROOT/'results'
DB='/home/ubuntu/openalgo/db/historify.duckdb'
con=duckdb.connect(DB,read_only=True)
spot=con.execute("SELECT timestamp,close FROM market_data WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'").df()
con.close()
spot['timestamp']=pd.to_datetime(spot.timestamp,unit='s',utc=True).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
spot=spot.drop_duplicates('timestamp',keep='last').set_index('timestamp').close
rows=[]
bands=[('all',0,float('inf')),('50_100',50,100),('100_150',100,150),('150_250',150,250),('250_500',250,500),('500_plus',500,float('inf'))]
for mins in [1,5,15]:
    p=OUT/f'events_{mins}m.csv'
    if not p.exists():continue
    ev=pd.read_csv(p,parse_dates=['sample_time','exit_time'])
    ev['session']=pd.to_datetime(ev.session).dt.date
    ev['spot_minute']=ev.sample_time.dt.floor('min')
    ev['spot']=ev.spot_minute.map(spot)
    ev=ev.dropna(subset=['spot']).copy()
    ev['signed_steps']=np.where(ev.option_type.eq('CE'),(ev.spot-ev.strike)/50,(ev.strike-ev.spot)/50)
    ev['step_bucket']=np.floor(ev.signed_steps+0.5).astype(int)
    ev['split']=np.where(ev.session<pd.Timestamp('2026-09-17').date(),'train',np.where(ev.session<pd.Timestamp('2026-09-23').date(),'validation','test'))
    for split,part in ev.groupby('split'):
        for name,lo,hi in bands:
            g=part[(part.premium>=lo)&(part.premium<=hi)]
            picked=core.replay(g,nearest=True)
            rows.append({'interval_min':mins,'split':split,'selector':'premium_band','bucket':name,'candidate_events':len(g),
                         **core.metric(picked),'test_stress_1pt':core.metric(picked,'net_points_stress1')['total_points'] if picked else None})
        for step in range(-6,7):
            g=part[part.step_bucket==step]
            if len(g)<3:continue
            picked=core.replay(g,nearest=True)
            label=f'{abs(step)}_ITM' if step>0 else (f'{abs(step)}_OTM' if step<0 else 'ATM')
            rows.append({'interval_min':mins,'split':split,'selector':'moneyness_step','bucket':label,'candidate_events':len(g),
                         **core.metric(picked),'test_stress_1pt':core.metric(picked,'net_points_stress1')['total_points'] if picked else None})
out=pd.DataFrame(rows)
out.to_csv(OUT/'strike_premium_breakdown.csv',index=False)
print(out[(out.interval_min==5)&(out.split.isin(['validation','test']))].to_string(index=False))
