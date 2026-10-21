from pathlib import Path
import duckdb,pandas as pd,numpy as np,re,json,sqlite3
from datetime import datetime,timedelta
BASE=Path("/home/ubuntu/openalgo/strategies/box-arbitrage/research")
BASE.mkdir(parents=True,exist_ok=True)
import os
PIN=os.getenv("BOX_RESEARCH_PIN","")
if PIN: BASE=BASE/("pinned_"+PIN);BASE.mkdir(exist_ok=True)
c=duckdb.connect("/home/ubuntu/openalgo/db/historify.duckdb",read_only=True)
df=c.execute("select symbol,timestamp,open,close,volume from market_data where interval='1m' and (symbol='NIFTY' or (exchange='NFO' and symbol like 'NIFTY%'))").df()
parts=[df]
for path in Path("/home/ubuntu/openalgo/strategies/scripts/historical_option_data/NIFTY/29SEP26/1m").glob("*.csv"):
 a=pd.read_csv(path)
 if not {"timestamp","open","close","volume"}.issubset(a):continue
 dt=pd.to_datetime(a.timestamp,errors="coerce")
 if dt.dt.tz is None:dt=dt.dt.tz_localize("Asia/Kolkata")
 a["timestamp"]=dt.astype("int64")//1000000000
 a["symbol"]=path.stem
 parts.append(a[["symbol","timestamp","open","close","volume"]])
df=pd.concat(parts,ignore_index=True).drop_duplicates(["symbol","timestamp"],keep="first")
df["dt"]=pd.to_datetime(df.timestamp,unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
meta={}
for s in df.symbol.unique():
 m=re.fullmatch(r"NIFTY(\d{2}[A-Z]{3}\d{2})(\d+)(CE|PE)",s)
 if m: meta[s]=(datetime.strptime(m[1],"%d%b%y").date(),int(m[2]),m[3],m[1])
con=sqlite3.connect("file:/home/ubuntu/openalgo/db/openalgo.db?mode=ro",uri=True)
lots=dict(con.execute("select symbol,lotsize from symtoken where exchange='NFO' and name='NIFTY'").fetchall())
observed_lots=sorted(set(int(v) for v in lots.values() if v))
print("Lot sizes in current master:",observed_lots,flush=True)
# Current instrument master is not historical: use 65 units explicitly as study assumption.
Q=65
lookup={(v[0],v[1],v[2]):s for s,v in meta.items()}
expiries=sorted(set(v[0] for v in meta.values()))
op=df[df.symbol.isin(meta)].copy()
opens=op.pivot(index="timestamp",columns="symbol",values="open")
closes=op.pivot(index="timestamp",columns="symbol",values="close")
vol=op.pivot(index="timestamp",columns="symbol",values="volume")
spot=df[df.symbol=="NIFTY"].sort_values("timestamp").drop_duplicates("timestamp").copy()
days=sorted(spot.dt.dt.date.unique())
def fees(prices,sides,b):
 buy=sum(p*Q for p,a in zip(prices,sides) if a==1)
 sell=sum(p*Q for p,a in zip(prices,sides) if a==-1)
 turnover=buy+sell
 txn=turnover*.000355299
 ipft=turnover*.000000001
 sebi=turnover*.000001
 brokerage=4*b
 return brokerage+txn+ipft+sebi+sell*.0015+buy*.00003+.18*(brokerage+txn+ipft+sebi)
sides=np.array([1,1,-1,-1])
tables={id(t):(t.to_numpy(dtype=float),{ts:i for i,ts in enumerate(t.index)},{sym:i for i,sym in enumerate(t.columns)}) for t in (opens,closes,vol)}
def prices(table,t,syms,require_volume=False):
 arr,rows,cols=tables[id(table)]
 i=rows.get(t)
 if i is None:return None
 ix=[cols[s] for s in syms]
 p=arr[i,ix]
 if not np.isfinite(p).all() or (p<=0).any():return None
 if require_volume:
  va,vr,vc=tables[id(vol)]
  vv=va[vr[t],[vc[s] for s in syms]]
  if not np.isfinite(vv).all() or (vv<=0).any():return None
 return p
all_results=[]
for slip,brokerage,model in [(.1,0,"kotak_api"),(.1,10,"standard_10"),(.5,0,"slippage_050"),(1.,0,"slippage_100"),(.1,0,"original_budget")]:
 daily=[];trades=[];quartets=0;signals=0
 def cost(p,a): 
  if model=="original_budget":return 4*30+sum(p)*Q*.003+100
  return fees(p,a,brokerage)
 for day in days:
  nearest=datetime.strptime(PIN,"%d%b%y").date() if PIN else next((e for e in expiries if e>=day),None)
  if nearest is not None and nearest<day:nearest=None
  records=spot[spot.dt.dt.date==day]
  row={"date":str(day),"expiry":str(nearest),"status":"no_signal","net_pnl":0.,"gross_pnl":0.,"charges":0.,"funding":0.,"eligible_quartets":0}
  for r in records.itertuples():
   clock=r.dt.strftime("%H:%M")
   if not ("09:20"<=clock<"14:30") or nearest is None: continue
   atm=int(np.floor(r.close/50+.5)*50)
   best=None
   for low in range(atm-250,atm+251,50):
    for width in (100,200):
     high=low+width
     if high>atm+250:continue
     syms=[lookup.get((nearest,low,"CE")),lookup.get((nearest,high,"PE")),lookup.get((nearest,high,"CE")),lookup.get((nearest,low,"PE"))]
     if None in syms:continue
     p=prices(closes,r.timestamp,syms,True)
     if p is None:continue
     quartets+=1;row["eligible_quartets"]+=1
     funding=50000*.10*max((nearest-day).days,1)/365
     edge=(width-np.dot(p,sides))*Q-cost(p,sides)-cost(p,-sides)-funding-4*Q*slip
     if edge>=200 and (best is None or edge>best[0]):best=(edge,syms,width,low,funding)
   if best is None:continue
   signals+=1
   edge,syms,width,low,funding=best
   entry_t=r.timestamp+60
   p=prices(opens,entry_t,syms,True)
   row.update(signal_time=r.dt.isoformat(),signal_edge=edge,lower_strike=low,width=width)
   if p is None:
    row["status"]="unresolved_entry";row["net_pnl"]=None;break
   entry=p+sides*slip
   # Only debit/premium cash proxy; historical basket margin is unavailable.
   if (entry[:2].sum()*Q+cost(entry,sides))*1.25>50000:
    row["status"]="capital_proxy_rejected";break
   entry_fee=cost(entry,sides)
   resolved=False;last_t=entry_t
   for exit_t in opens.index[(opens.index>entry_t)&(opens.index<=int((r.dt.normalize()+pd.Timedelta(hours=15,minutes=29)).timestamp()))]:
    last=prices(closes,exit_t,syms,True)
    if last is None: continue
    estimated=last-sides*slip
    elapsed_funding=50000*.10*(exit_t+60-entry_t)/(365*86400)
    estimate=np.dot(estimated-entry,sides)*Q-entry_fee-cost(estimated,-sides)-elapsed_funding
    exitclock=pd.Timestamp(exit_t,unit="s",tz="UTC").tz_convert("Asia/Kolkata").strftime("%H:%M")
    gap=exit_t-last_t>60
    last_t=exit_t
    reason="DATA_GAP" if gap else "TARGET" if estimate>=100 else "STOP" if estimate<=-1000 else "EOD" if exitclock>="15:20" else None
    if reason is None:continue
    actual=prices(opens,exit_t+60,syms,True)
    if actual is None:
     row["status"]="unresolved_exit";row["net_pnl"]=None;break
    exitp=actual-sides*slip
    if (exitp<=0).any():
     row["status"]="unresolved_exit";row["net_pnl"]=None;break
    gross=np.dot(exitp-entry,sides)*Q
    charges=entry_fee+cost(exitp,-sides)
    row.update(status="closed",net_pnl=gross-charges-elapsed_funding,gross_pnl=gross,charges=charges,funding=elapsed_funding,
       entry_time=pd.Timestamp(entry_t,unit="s",tz="UTC").tz_convert("Asia/Kolkata").isoformat(),
       exit_time=pd.Timestamp(exit_t+60,unit="s",tz="UTC").tz_convert("Asia/Kolkata").isoformat(),reason=reason)
    trades.append(dict(row,contracts="|".join(syms),entry_prices=entry.tolist(),exit_prices=exitp.tolist()))
    resolved=True;break
   if not resolved and row["status"]=="no_signal":row["status"]="unresolved_exit";row["net_pnl"]=None
   break
  if row["eligible_quartets"]==0: row["status"]="insufficient_data";row["net_pnl"]=None
  daily.append(row)
 out=pd.DataFrame(daily)
 out.to_csv(BASE/(model+"_daily.csv"),index=False)
 pd.DataFrame(trades).to_csv(BASE/(model+"_trades.csv"),index=False)
 closed=out[out.status=="closed"]
 result={"model":model,"slippage_per_leg_per_side":slip,"brokerage_per_order":brokerage,"days":len(days),"closed_boxes":len(closed),
 "unresolved_days":int(out.status.str.startswith("unresolved").sum()),"net_resolved":round(float(closed.net_pnl.sum()),2),
 "gross_resolved":round(float(closed.gross_pnl.sum()),2),"charges":round(float(closed.charges.sum()),2),
 "funding":round(float(closed.funding.sum()),2),"positive_boxes":int((closed.net_pnl>0).sum()),
 "eligible_quartets":quartets,"signal_days":signals,"statuses":out.status.value_counts().to_dict()}
 all_results.append(result);print(result,flush=True)
audit={"rows":len(df),"option_symbols":len(meta),"pinned_expiry":PIN,"spot_days":len(days),"start":str(df.dt.min()),"end":str(df.dt.max()),
"lot_units_assumption":Q,"current_master_lot_sizes":observed_lots,"expiries":[str(x) for x in expiries],
"historical_depth_available":False,"method":"Completed minute close signals, next-minute open entry/exit; equal quantities; positive volume all four bars; nearest expiry present in archive; one attempt/day. Original budget and Kotak fees compared. Slippage sensitivities predeclared.",
"limitations":["No historical bid/ask, order book, margin or fill sequence. Simultaneous four-leg fill assumption.",
"Minute prices are separate last trades; synchronized timestamps do not guarantee executable quotes.",
"Nearest expiry in available archive may differ from actual nearest listed expiry; archive coverage incomplete.",
"65-unit lot assumption; empty historical metadata. Current master is not historical proof.",
"Unresolved entries/exits excluded from resolved P&L and explicitly counted.",
"Capital proxy is long premiums times 1.25, not staged basket-margin verification.",
"10% capital opportunity cost through expiry in entry gate; elapsed holding-time opportunity cost deducted at exit using conservative 50000 capital proxy.",
"No parameter optimization; no orders sent."]}
(BASE/"summary.json").write_text(json.dumps({"audit":audit,"results":all_results},indent=2))
try:
 import matplotlib;matplotlib.use("Agg")
 import matplotlib.pyplot as plt
 d=pd.read_csv(BASE/"kotak_api_daily.csv")
 fig,ax=plt.subplots(figsize=(12,4));x=pd.to_datetime(d.date)
 colors=["#4c78a8" if v>=0 else "#e45756" for v in d.net_pnl.fillna(0)]
 ax.bar(x,d.net_pnl.fillna(0),color=colors)
 missing=d.net_pnl.isna();ax.scatter(x[missing],[0]*sum(missing),marker="x",color="black",label="Unresolved (not zero profit)")
 ax.axhline(0,color="gray",linewidth=.7);ax.set(title="Box spread: OHLC approximation only — Kotak API fees, 65 units",ylabel="Resolved net P&L (INR)",xlabel="Date")
 ax.legend();fig.autofmt_xdate();fig.tight_layout();fig.savefig(BASE/"daily_pnl.png",dpi=160)
except ImportError:pass
print("OUTPUT",BASE,flush=True)

