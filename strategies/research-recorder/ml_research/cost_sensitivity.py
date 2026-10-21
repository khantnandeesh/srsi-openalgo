from pathlib import Path
import sys,duckdb,numpy as np,pandas as pd
sys.path.insert(0,'/home/ubuntu/openalgo/strategies/research-recorder/ml_research')
import run_srsi_ml_research as core
ROOT=Path('/home/ubuntu/openalgo/strategies/research-recorder/ml_research')
con=duckdb.connect('/home/ubuntu/openalgo/db/historify.duckdb',read_only=True)
s=con.execute("select timestamp,close from market_data where symbol='NIFTY' and exchange='NSE_INDEX' and interval='1m'").df();con.close()
s['timestamp']=pd.to_datetime(s.timestamp,unit='s',utc=True).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
s=s.drop_duplicates('timestamp',keep='last').set_index('timestamp').close
def costs(entry,exitp,qty=65):
    # NSE index options; current public retail tariff proxy, with one entry and one exit order.
    buy=float(entry)*qty;sell=float(exitp)*qty
    brokerage=40.0
    transaction=(buy+sell)*0.0003553
    sebi=(buy+sell)*0.000001
    stamp=buy*0.00003
    stt=sell*0.0015
    gst=(brokerage+transaction+sebi)*0.18
    return (brokerage+transaction+sebi+stamp+stt+gst)/qty
out=[]
for mins in [1,5,15]:
    ev=pd.read_csv(ROOT/f'results/events_{mins}m.csv',parse_dates=['sample_time','exit_time'])
    ev['session']=pd.to_datetime(ev.session).dt.date
    ev['spot']=ev.sample_time.dt.floor('min').map(s)
    ev=ev.dropna(subset=['spot']).copy()
    ev['signed_steps']=np.where(ev.option_type.eq('CE'),(ev.spot-ev.strike)/50,(ev.strike-ev.spot)/50)
    ev['step_bucket']=np.floor(ev.signed_steps+0.5).astype(int)
    ev['split']=np.where(ev.session<pd.Timestamp('2026-09-17').date(),'train',np.where(ev.session<pd.Timestamp('2026-09-23').date(),'validation','test'))
    for split,p in ev.groupby('split'):
        policies=[('all_nearest',p),('premium50_100',p[(p.premium>=50)&(p.premium<=100)])]
        if mins==5:
            for step in [-6,-5,-4,-3,0,3,5]:
                policies.append((f'moneyness_{step}',p[p.step_bucket==step]))
        for name,g in policies:
            chosen=core.replay(g,nearest=True)
            if not chosen:continue
            d=pd.DataFrame(chosen)
            fee=d.apply(lambda r:costs(r.entry_price,r.exit_price),axis=1)
            pnl_fee_1=d.gross_points-1.0-fee
            out.append({'interval_min':mins,'split':split,'policy':name,'trades':len(d),
                        'gross_points':round(float((d.exit_price-d.entry_price).sum()),2),
                        'net_after_1pt_sell_slip_points':round(float((d.exit_price-d.entry_price-1).sum()),2),
                        'modeled_fees_rupees_65qty':round(float((fee*65).sum()),2),
                        'net_after_1pt_slip_and_fees_points':round(float(pnl_fee_1.sum()),2),
                        'mean_fee_points_per_trade':round(float(fee.mean()),3),
                        'mean_signal_premium':round(float(d.entry_price.mean()),2),
                        'median_signal_premium':round(float(d.entry_price.median()),2),
                        'positive_trading_days':int((d.assign(net=pnl_fee_1).groupby('session').net.sum()>0).sum()),
                        'active_days':int(d.session.nunique())})
pd.DataFrame(out).to_csv(ROOT/'results/cost_sensitivity.csv',index=False)
print(pd.DataFrame(out).query("interval_min == 5 and split in ['validation','test']").to_string(index=False))
