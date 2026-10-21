#!/usr/bin/env python3
"""VM-only causal option/spot features for a declared small strategy study."""
from pathlib import Path
import sys,json,time,re
import numpy as np,pandas as pd,duckdb
ROOT=Path("/home/ubuntu/openalgo")
RESEARCH=ROOT/"strategies/research-recorder/ml_research"
OUT=RESEARCH/"results/liquid_strategy_study"
OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(RESEARCH))
import run_srsi_ml_research as core
RULES=[
 {"id":"srsi_zero","signal":"zero","trend":False,"exit":"srsi"},
 {"id":"recovery5","signal":"recovery5","trend":False,"exit":"srsi"},
 {"id":"recovery20","signal":"recovery20","trend":False,"exit":"srsi"},
 {"id":"trend_recovery5","signal":"recovery5","trend":True,"exit":"srsi"},
 {"id":"trend_recovery20","signal":"recovery20","trend":True,"exit":"srsi"},
 {"id":"trend_recovery_target","signal":"recovery20","trend":True,"exit":"target"},
 {"id":"spot_breakout","signal":"breakout","trend":True,"exit":"target"},
 {"id":"trend_pullback","signal":"pullback","trend":True,"exit":"target"},
]
PLAN=dict(hypotheses=RULES,declared_at_utc=pd.Timestamp.now(tz="UTC").isoformat(),
    universe="Nearest archived unexpired expiry excluding expiry day, positive premium80..150, OTM offsets0..6, select closest premium115",
    option_liquidity="Positive current volume, at least9observations and325units in prior10elapsedminutes",
    indicators="5m provisional OpenAlgo-equivalent StochRSI4,4,2,2; completed5m spotEMA20slope; previous12completed5m range",
    entries="Signals at1mclose; nextminuteopen+0.25point; missingnextopen flagged signalclose assumption",
    exits="Strict15point stop; SRSI>=80at>=entry or target30point/30minmaxhold; daily15:25exit",
    limits="5fixedlots325units; max3trades per CE/PE side/day;5mincooldown; lock exact contract",
    costs="20brokerage/order; NSEtransactionproxy0.03553%,STT0.15%sales,SEBI0.0001%,GST18%,stamp0.003%buys;1point sellslip",
    periods={"development":["2026-09-01","2026-09-11"],"validation":["2026-09-15","2026-09-18"],
             "retrospective_evaluation":["2026-09-21","2026-09-24"],"additional_day":"2026-10-01"},
    selection="Require positive development andvalidation, >=10/5trades respectively, no unresolved exits; choose largest minimum mean daily net; freeze before evaluation",
    warning="Septemberevaluation dates have been used in priorresearch; not a truly untouched holdout. October1is onlyone additionalday, insufficient to establish live profitability.")
(OUT/"declared_plan.json").write_text(json.dumps(PLAN,indent=2))
def normal_timestamp(x):
    s=pd.to_datetime(x,format="mixed",errors="coerce")
    if isinstance(s.dtype,pd.DatetimeTZDtype):
        s=s.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return s
def load_options():
    parts=[]
    for p in sorted((ROOT/"strategies/scripts/historical_option_data/NIFTY/29SEP26/1m").glob("*.csv")):
        d=pd.read_csv(p)
        if not {"timestamp","open","high","low","close"}.issubset(d):continue
        d["symbol"]=p.stem;d["timestamp"]=normal_timestamp(d.timestamp)
        if "volume" not in d:d["volume"]=0
        for c in ["open","high","low","close","volume"]:
            d[c]=pd.to_numeric(d[c],errors="coerce")
        parts.append(d[["symbol","timestamp","open","high","low","close","volume"]])
    cached=pd.read_parquet(RESEARCH/"results/expiry_locked_6otm_5lots_bars.parquet")
    cached=cached[cached.expiry_code.eq("06OCT26")]
    parts.append(cached[["symbol","timestamp","open","high","low","close","volume"]])
    additional=RESEARCH/"results/prospective_20261001"
    if (additional/"download_status.json").exists():
        status=json.loads((additional/"download_status.json").read_text())
        if status.get("finished"):
            for p in sorted(additional.glob("NIFTY06OCT26*.csv")):
                d=pd.read_csv(p);d["symbol"]=p.stem;d["timestamp"]=normal_timestamp(d.timestamp)
                for c in ["open","high","low","close","volume"]:d[c]=pd.to_numeric(d[c],errors="coerce")
                parts.append(d[["symbol","timestamp","open","high","low","close","volume"]])
    opt=pd.concat(parts,ignore_index=True).dropna(subset=["timestamp","close"])
    opt=opt[(opt.open>0)&(opt.high>0)&(opt.low>0)&(opt.close>0)]
    opt=opt.drop_duplicates(["symbol","timestamp"],keep="last").sort_values(["symbol","timestamp"])
    metadata=[]
    for symbol in opt.symbol.unique():
        parsed=core.parse_symbol(symbol)
        if parsed:metadata.append((symbol,*parsed))
    opt=opt.merge(pd.DataFrame(metadata,columns=["symbol","expiry","strike","option_type"]),on="symbol")
    opt["session"]=opt.timestamp.dt.date
    opt=opt[(opt.timestamp.dt.time>=pd.Timestamp("09:15").time())&(opt.timestamp.dt.time<pd.Timestamp("15:30").time())]
    opt["effective_time"]=opt.timestamp+pd.Timedelta(minutes=1)
    return opt.sort_values(["symbol","timestamp"])
def load_spot():
    con=duckdb.connect(str(ROOT/"db/historify.duckdb"),read_only=True)
    d=con.execute("select timestamp,open,high,low,close,volume from market_data where symbol='NIFTY' and exchange='NSE_INDEX' and interval='1m'").fetchdf();con.close()
    d["timestamp"]=pd.to_datetime(d.timestamp,unit="s",utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    p=RESEARCH/"results/prospective_20261001/NIFTY.csv"
    if p.exists():
        extra=pd.read_csv(p);extra["timestamp"]=normal_timestamp(extra.timestamp);d=pd.concat([d,extra],ignore_index=True)
    d=d.drop_duplicates("timestamp",keep="last").dropna(subset=["close"]).sort_values("timestamp")
    d=d[(d.timestamp.dt.time>=pd.Timestamp("09:15").time())&(d.timestamp.dt.time<pd.Timestamp("15:30").time())]
    return d
def spot_features(spot):
    s=spot.copy()
    s["effective_time"]=s.timestamp+pd.Timedelta(minutes=1)
    anchors=s.timestamp.dt.normalize()+pd.Timedelta(hours=9,minutes=15)
    n=((s.timestamp-anchors).dt.total_seconds()//300).astype(int)
    s["bucket_end"]=anchors+pd.to_timedelta((n+1)*5,unit="m")
    g=s.groupby("bucket_end",sort=True).agg(open=("open","first"),high=("high","max"),low=("low","min"),close=("close","last"),bars=("close","size"),last_start=("timestamp","max"))
    g=g[(g.bars>=4)&(g.last_start+pd.Timedelta(minutes=1)==g.index)]
    g["ema20"]=g.close.ewm(span=20,adjust=False,min_periods=20).mean()
    g["ema20_prev"]=g.ema20.shift(1)
    g["prior_high12"]=g.high.rolling(12,min_periods=12).max().shift(1)
    g["prior_low12"]=g.low.rolling(12,min_periods=12).min().shift(1)
    g["range_complete12"]=g.bars.rolling(12,min_periods=12).sum().shift(1).eq(60)
    g=g.reset_index()
    merged=pd.merge_asof(s.sort_values("effective_time"),g[["bucket_end","ema20","ema20_prev","prior_high12","prior_low12","range_complete12"]].sort_values("bucket_end"),
                        left_on="effective_time",right_on="bucket_end",direction="backward",tolerance=pd.Timedelta(minutes=5))
    merged["spot"]=merged.close
    merged["trend_up"]=(merged.spot>merged.ema20)&(merged.ema20>merged.ema20_prev)
    merged["trend_down"]=(merged.spot<merged.ema20)&(merged.ema20<merged.ema20_prev)
    prior_contiguous=merged.timestamp.diff().eq(pd.Timedelta(minutes=1))
    above=merged.spot>merged.prior_high12
    below=merged.spot<merged.prior_low12
    merged["breakout_up"]=merged.range_complete12.eq(True)&above&~above.shift(1,fill_value=False)&prior_contiguous&(merged.spot>merged.spot.shift(1))
    merged["breakout_down"]=merged.range_complete12.eq(True)&below&~below.shift(1,fill_value=False)&prior_contiguous&(merged.spot<merged.spot.shift(1))
    return merged[["timestamp","spot","ema20","ema20_prev","trend_up","trend_down","breakout_up","breakout_down"]]
def option_features(g):
    g=g.sort_values("timestamp").copy().reset_index(drop=True)
    state=core.StochState();last_bucket=None;last_close=None;ks=[];ds=[];warm=[]
    for row in g.itertuples(index=False):
        key=(row.timestamp.date(),int((row.timestamp.hour*60+row.timestamp.minute-555)//5))
        if last_bucket is not None and key!=last_bucket:
            state.finalize(last_close)
            state.rsi=state.rsi[-3:];state.raw=state.raw[-1:];state.k=state.k[-1:]
        last_bucket=key;last_close=float(row.close)
        k,d=state.provisional(last_close)
        ks.append(k);ds.append(d);warm.append(state.n>=20)
    g["k"]=ks;g["d"]=ds;g["warmed"]=warm
    g["prev_k"]=g.k.shift(1);g["prev_close"]=g.close.shift(1)
    contiguous=g.timestamp.diff().eq(pd.Timedelta(minutes=1))
    low_time=g.timestamp.where(g.k<=5).ffill()
    g["recent_low"]=((g.timestamp-low_time)<=pd.Timedelta(minutes=15))
    g["signal_zero"]=(g.k.abs()<1e-10)&(g.prev_k.abs()>=1e-10)&contiguous
    g["signal_recovery5"]=(g.prev_k<=5)&(g.k>5)&(g.k<=65)&g.recent_low&contiguous
    g["signal_recovery20"]=(g.prev_k<=20)&(g.k>20)&(g.k<=65)&g.recent_low&contiguous
    g["signal_pullback"]=(g.close>g.prev_close)&(g.prev_k<=20)&(g.k>g.prev_k)&contiguous
    g["observed10"]=0.;g["volume10"]=0.
    for day,ids in g.groupby("session",sort=False).groups.items():
        d=g.loc[ids];idx=pd.DatetimeIndex(d.timestamp)
        g.loc[ids,"observed10"]=pd.Series(np.ones(len(d)),index=idx).rolling("10min").sum().to_numpy()
        g.loc[ids,"volume10"]=pd.Series(d.volume.to_numpy(),index=idx).rolling("10min").sum().to_numpy()
    return g
def main():
    start=time.monotonic();opt=load_options();spot=load_spot();features=spot_features(spot)
    print("PREPARE",len(opt),"optionbars",opt.symbol.nunique(),"symbols",len(spot),"spotbars",flush=True)
    chunks=[]
    for number,(symbol,g) in enumerate(opt.groupby("symbol",sort=True),1):
        chunks.append(option_features(g))
        if number%40==0:print("FEATURES",number,"symbols elapsed",round(time.monotonic()-start,1),flush=True)
    bars=pd.concat(chunks,ignore_index=True).merge(features,on="timestamp",how="left")
    bars["atm"]=np.floor(bars.spot/50+.5)*50
    bars["offset"]=np.where(bars.option_type.eq("CE"),(bars.strike-bars.atm)/50,(bars.atm-bars.strike)/50)
    bars["dte"]=(pd.to_datetime(bars.expiry)-pd.to_datetime(bars.session)).dt.days
    # Expiry chosen by calendar BEFORE price, liquidity, or signal checks.
    catalog=sorted(bars.expiry.unique())
    nearest={day:next((e for e in catalog if e>day),None) for day in bars.session.unique()}
    bars["chosen_expiry"]=bars.session.map(nearest)
    bars["eligible"]=(bars.expiry.eq(bars.chosen_expiry)&bars.dte.between(1,35)&
        bars.offset.between(0,6)&bars.close.between(80,150)&(bars.volume>0)&
        (bars.observed10>=9)&(bars.volume10>=325)&bars.spot.notna()&bars.warmed)
    bars.to_parquet(OUT/"causal_features.parquet",index=False)
    coverage=bars.groupby(["session","expiry","option_type"]).agg(bars=("symbol","size"),eligible_observations=("eligible","sum"),symbols=("symbol","nunique")).reset_index()
    coverage.to_csv(OUT/"coverage.csv",index=False)
    features.to_parquet(OUT/"spot_features.parquet",index=False)
    print("PREPARE_DONE",len(bars),"eligible",int(bars.eligible.sum()),"elapsed",round(time.monotonic()-start,1),flush=True)
if __name__=="__main__":main()

