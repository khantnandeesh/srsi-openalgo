#!/usr/bin/env python3
"""Three preregistered exploratory structural hypotheses; VM-only, no orders."""
from pathlib import Path
from datetime import time as dt_time
import json,time
import numpy as np,pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from openalgo import ta
import prepare_liquid_strategy_study as prep
import run_liquid_strategy_study as base

ROOT=Path("/home/ubuntu/openalgo/strategies/research-recorder/ml_research")
OUT=ROOT/"results/structural_strategy_study"
OUT.mkdir(parents=True,exist_ok=True)
RULES=[
 {"id":"adx_recovery20","description":"Completed5m spotADX14>=25,+DI/-DIdirection;optionKcrossabove20afterK<=5in15min","max_trades":3},
 {"id":"opening_range_breakout","description":"Frozen09:15-09:45spotrange;firstqualifying1mclosebreakoutperdirection/day","max_trades":1},
 {"id":"squeeze_breakout","description":"Lastcompleted5mBB20,2populationSTDbandwidth<=prior100bar20thpercentile;1mclosebandcross","max_trades":3},
]
PLAN=dict(declared_at_utc=pd.Timestamp.now(tz="UTC").isoformat(),rules=RULES,
 universe="Nearestarchivedexpiryexcludingexpiryday;premium80..150;OTMoffset0..6;selectclosest115BEFOREsignalcheck",
 selection="RequirepositivenetdevSep1-11andvalSep15-18;>=10dev/5valtrades;zero unresolved;maxexitstaleness<=2min;freezehighestminimummeandailynet",
 option_atr="OpenAlgoATR14onfullcontractcompleted5mbars;>=4minuteobservationsandlastminuteendingexactlyatbucketend;featureasof<=signal",
 exits="R=clip(1.5*ATR,7,15);target2R;60minuteholdingor15:25earlier;stopcheckedfirst;breakevenfromnextbarafterminuteclose>=entry+R",
 execution="5lots325units,nextminuteopen+0.25buy-slip,-1sell-slip;missingnextopenanddeadlinepricesflaggedlastcloseassumptions;5mincooldown",
 warning="ALLdatesarealreadyexposedexploratoryretrospectiveobservations;noneisgenuineuntouchedholdout;no tickqueue/5secondlimitfillverification;missingbarsexitpathrisk",
 periods={"development":"2026-09-01..11","validation":"2026-09-15..18","later_retrospective":"2026-09-21..24","additional_day":"2026-10-01"})
(OUT/"declared_plan.json").write_text(json.dumps(PLAN,indent=2))

def aggregate5(g):
    g=g.sort_values("timestamp").copy()
    anchor=g.timestamp.dt.normalize()+pd.Timedelta(hours=9,minutes=15)
    bucket=((g.timestamp-anchor).dt.total_seconds()//300).astype(int)
    g["bucket_end"]=anchor+pd.to_timedelta((bucket+1)*5,unit="m")
    a=g.groupby("bucket_end",sort=True).agg(open=("open","first"),high=("high","max"),low=("low","min"),close=("close","last"),bars=("close","size"),last_start=("timestamp","max"))
    return a[(a.bars>=4)&(a.last_start+pd.Timedelta(minutes=1)==a.index)].copy()

def option_features(g):
    g=g.sort_values("timestamp").reset_index(drop=True).copy()
    a=aggregate5(g)
    a["option_atr14"]=np.asarray(ta.atr(a.high,a.low,a.close,period=14)) if len(a)>=14 else np.nan
    a=a.reset_index().rename(columns={"bucket_end":"option_atr_available_time"})
    g=pd.merge_asof(g,a[["option_atr_available_time","option_atr14"]],left_on="effective_time",right_on="option_atr_available_time",direction="backward",tolerance=pd.Timedelta(minutes=5))
    previous=g.k.shift(1)
    low_time=g.timestamp.where(g.k<=5).ffill()
    recent=(g.timestamp-low_time)<=pd.Timedelta(minutes=15)
    contiguous=g.timestamp.diff().eq(pd.Timedelta(minutes=1))
    g["recovery20_exact"]=(previous<=20)&(g.k>20)&recent&contiguous
    return g

def spot_features(s):
    s=s.sort_values("timestamp").copy()
    s["effective_time"]=s.timestamp+pd.Timedelta(minutes=1)
    a=aggregate5(s)
    plus,minus,adx=ta.adx(a.high,a.low,a.close,period=14)
    a["di_plus"]=np.asarray(plus);a["di_minus"]=np.asarray(minus);a["adx14"]=np.asarray(adx)
    mid=a.close.rolling(20,min_periods=20).mean()
    std=a.close.rolling(20,min_periods=20).std(ddof=0)
    a["upper_band"]=mid+2*std;a["lower_band"]=mid-2*std
    width=(a.upper_band-a.lower_band)/mid
    a["squeeze"]=width.le(width.shift(1).rolling(100,min_periods=100).quantile(.2))
    a=a.reset_index().rename(columns={"bucket_end":"spot_feature_available_time"})
    s=pd.merge_asof(s,a[["spot_feature_available_time","di_plus","di_minus","adx14","upper_band","lower_band","squeeze"]],left_on="effective_time",right_on="spot_feature_available_time",direction="backward",tolerance=pd.Timedelta(minutes=5))
    session=s.timestamp.dt.date
    in_or=(s.timestamp.dt.time>=dt_time(9,15))&(s.timestamp.dt.time<dt_time(9,45))
    ranges=s[in_or].groupby(session[in_or]).agg(or_high=("high","max"),or_low=("low","min"),or_bars=("close","size"))
    s["or_high"]=session.map(ranges.or_high);s["or_low"]=session.map(ranges.or_low);s["or_complete"]=session.map(ranges.or_bars).eq(30)
    prior=s.close.shift(1);contiguous=s.timestamp.diff().eq(pd.Timedelta(minutes=1))
    after_or=s.effective_time.dt.time>=dt_time(9,45)
    s["or_up"]=s.or_complete&after_or&contiguous&(prior<=s.or_high)&(s.close>s.or_high)
    s["or_down"]=s.or_complete&after_or&contiguous&(prior>=s.or_low)&(s.close<s.or_low)
    s["squeeze_up"]=s["squeeze"].eq(True)&contiguous&(prior<=s.upper_band)&(s.close>s.upper_band)
    s["squeeze_down"]=s["squeeze"].eq(True)&contiguous&(prior>=s.lower_band)&(s.close<s.lower_band)
    s["adx_up"]=(s.adx14>=25)&(s.di_plus>s.di_minus)
    s["adx_down"]=(s.adx14>=25)&(s.di_minus>s.di_plus)
    cols=["timestamp","spot_feature_available_time","di_plus","di_minus","adx14","or_high","or_low","or_up","or_down","upper_band","lower_band","squeeze_up","squeeze_down","adx_up","adx_down"]
    return s[cols]

def signals(chosen,rule,days):
    e=chosen[chosen.session.isin(days)].copy()
    if rule["id"]=="adx_recovery20":
        trigger=e.recovery20_exact.eq(True)&np.where(e.option_type.eq("CE"),e.adx_up.eq(True),e.adx_down.eq(True))
    elif rule["id"]=="opening_range_breakout":
        trigger=np.where(e.option_type.eq("CE"),e.or_up.eq(True),e.or_down.eq(True))
    else:
        trigger=np.where(e.option_type.eq("CE"),e.squeeze_up.eq(True),e.squeeze_down.eq(True))
    # ATR validity checked after contract choice; no indicator-driven fallback.
    return e[trigger&e.option_atr14.gt(0)&np.isfinite(e.option_atr14)].sort_values(["effective_time","option_type"])

def outcome(row,path,sell_slip=1.,delay=0):
    entry_time=row.effective_time+pd.Timedelta(minutes=delay)
    starts=path.timestamp
    ix=np.flatnonzero(starts.eq(entry_time).to_numpy())
    assumed_entry=not len(ix)
    if len(ix):
        entry_i=int(ix[0]);base_entry=float(path.open.iloc[entry_i])
        entry_quote_available_time=entry_time
    else:
        known=path[path.effective_time<=entry_time]
        if known.empty:raise AssertionError("No causallyavailable entry quote")
        entry_i=int(np.searchsorted(starts.to_numpy(),np.datetime64(entry_time),side="left"))
        base_entry=float(known.close.iloc[-1]);entry_quote_available_time=known.effective_time.iloc[-1]
    entry=base_entry+base.ENTRY_SLIP
    risk=float(np.clip(1.5*row.option_atr14,7,15))
    initial_stop=entry-risk;stop=initial_stop;target=entry+2*risk
    deadline=min(entry_time+pd.Timedelta(minutes=60),entry_time.normalize()+pd.Timedelta(hours=15,minutes=25))
    px=None;exit_time=None;reason=None;source_i=None;assumed_exit=False
    breakeven_armed_at=pd.NaT;ambiguous=False
    for j in range(entry_i,len(path)):
        r=path.iloc[j]
        if r.effective_time>deadline:break
        if float(r.low)<stop:
            ambiguous=bool(float(r.high)>=target)
            px=min(float(r.open),stop);reason="BREAKEVEN_STOP" if stop==entry else "STOP"
        elif float(r.high)>=target:
            px=target;reason="TARGET_2R"
        elif r.effective_time==deadline:
            px=float(r.close);reason="TIME_LIMIT"
        if px is not None:
            exit_time=r.effective_time;source_i=j;break
        # Changing stop here takes effect only when evaluating the NEXT bar.
        if float(r.close)>=entry+risk and pd.isna(breakeven_armed_at):
            stop=entry;breakeven_armed_at=r.effective_time
    if px is None:
        known=path[path.effective_time<=deadline]
        if known.empty:raise AssertionError("No exit history")
        source_i=int(known.index[-1]);px=float(known.close.iloc[-1]);exit_time=deadline
        reason="ASSUMED_DEADLINE_LAST_CLOSE";assumed_exit=True
    source_time=path.effective_time.iloc[source_i]
    stale=max(0.,(exit_time-source_time).total_seconds()/60)
    p=path.effective_time[(path.effective_time>=entry_time)&(path.effective_time<=source_time)]
    gaps=p.diff().dropna().dt.total_seconds()/60
    entry_stale=max(0.,(entry_time-entry_quote_available_time).total_seconds()/60)
    gap_count=int((gaps>1).sum())+int(stale>1)
    sell_fill=px-sell_slip
    raw=(px-base_entry)*base.QTY;slip=(base.ENTRY_SLIP+sell_slip)*base.QTY;cost=base.fee(entry,sell_fill)
    return dict(entry_time=entry_time,exit_time=exit_time,entry_base=base_entry,entry_price=entry,risk_points=risk,initial_stop=initial_stop,target=target,
        exit_quote_price=px,exit_price=sell_fill,reason=reason,exit_complete=True,
        assumed_entry=assumed_entry,assumed_exit=assumed_exit,entry_staleness_minutes=entry_stale,
        exit_price_available_time=source_time,exit_staleness_minutes=stale,missing_minute_gaps=gap_count,
        largest_gap_minutes=max(float(gaps.max()) if len(gaps) else 0.,stale),
        breakeven_armed_at=breakeven_armed_at,stop_target_ambiguous=ambiguous,
        raw_gross_pnl=raw,slippage=slip,fees=cost,net_pnl=raw-slip-cost)

def replay(chosen,paths,rule,days,sell_slip=1.,delay=0):
    records=[];busy={};counts={}
    for row in signals(chosen,rule,days).itertuples(index=False):
        key=(row.session,row.option_type)
        if counts.get(key,0)>=rule["max_trades"]:continue
        if key in busy and row.effective_time<=busy[key]+pd.Timedelta(minutes=5):continue
        result=outcome(row,paths[(row.symbol,row.session)],sell_slip,delay)
        busy[key]=result["exit_time"];counts[key]=counts.get(key,0)+1
        records.append(dict(strategy=rule["id"],session=row.session,symbol=row.symbol,option_type=row.option_type,strike=int(row.strike),expiry=row.expiry,
            signal_time=row.effective_time,k=float(row.k),spot=float(row.spot),premium=float(row.close),option_atr14=float(row.option_atr14),
            option_atr_available_time=row.option_atr_available_time,spot_feature_available_time=row.spot_feature_available_time,**result))
    ledger=pd.DataFrame(records)
    if len(ledger):
        assert (ledger.option_atr_available_time<=ledger.signal_time).all()
        assert (ledger.spot_feature_available_time<=ledger.signal_time).all()
        assert (ledger.exit_price_available_time<=ledger.exit_time).all()
        for key,g in ledger.groupby(["session","option_type"]):
            g=g.sort_values("entry_time")
            assert not (g.entry_time.iloc[1:].to_numpy()<=g.exit_time.iloc[:-1].to_numpy()).any()
        be=ledger[ledger.breakeven_armed_at.notna()&ledger.reason.eq("BREAKEVEN_STOP")]
        assert (be.breakeven_armed_at<be.exit_time).all(),"Breakeven applied in same closedminute"
        assert np.allclose(ledger.raw_gross_pnl-ledger.slippage-ledger.fees,ledger.net_pnl)
    return ledger

def metrics(t,days):
    m=base.metrics(t,days)
    m["stop_target_ambiguous"]=int(t.stop_target_ambiguous.sum()) if len(t) else 0
    m["breakeven_stops"]=int(t.reason.eq("BREAKEVEN_STOP").sum()) if len(t) else 0
    return m

def main():
    began=time.monotonic()
    cols=["symbol","timestamp","effective_time","open","high","low","close","k","session","expiry","strike","option_type","eligible","volume10","spot"]
    bars=pd.read_parquet(base.OUT/"causal_features.parquet",columns=cols)
    spot=prep.load_spot()
    # All recorded spot sessions, including sessions with zero eligible options.
    study_days=sorted(set(spot.timestamp.dt.date))
    dev=[d for d in study_days if pd.Timestamp("2026-09-01").date()<=d<=pd.Timestamp("2026-09-11").date()]
    val=[d for d in study_days if pd.Timestamp("2026-09-15").date()<=d<=pd.Timestamp("2026-09-18").date()]
    later=[d for d in study_days if pd.Timestamp("2026-09-21").date()<=d<=pd.Timestamp("2026-09-24").date()]
    extra=[d for d in study_days if d==pd.Timestamp("2026-10-01").date()]
    chunks=[]
    for i,(symbol,g) in enumerate(bars.groupby("symbol",sort=True),1):
        chunks.append(option_features(g))
        if i%50==0:print("OPTION_ATR",i,"elapsed",round(time.monotonic()-began,1),flush=True)
    bars=pd.concat(chunks,ignore_index=True)
    sf=spot_features(spot)
    bars=bars.merge(sf,on="timestamp",how="left")
    paths,chosen=base.setup(bars)
    chosen.to_parquet(OUT/"chosen_contract_features.parquet",index=False)
    comparisons=[];ledgers=[]
    for rule in RULES:
        a=replay(chosen,paths,rule,dev);b=replay(chosen,paths,rule,val)
        ma=metrics(a,dev);mb=metrics(b,val)
        passed=(ma["net_pnl"]>0 and mb["net_pnl"]>0 and ma["trades"]>=10 and mb["trades"]>=5 and ma["unresolved"]==0 and mb["unresolved"]==0 and ma["max_exit_staleness_minutes"]<=2 and mb["max_exit_staleness_minutes"]<=2)
        comparisons.append(dict(strategy=rule["id"],qualifies=passed,score=min(ma["mean_daily_net"],mb["mean_daily_net"]),**{"development_"+k:v for k,v in ma.items()},**{"validation_"+k:v for k,v in mb.items()}))
        for period,t in [("development",a),("validation",b)]:
            if len(t):t["period"]=period;ledgers.append(t)
        print("CANDIDATE",rule["id"],json.dumps(dict(development=ma,validation=mb,qualifies=passed),default=str),flush=True)
    table=pd.DataFrame(comparisons)
    table.to_csv(OUT/"development_validation_comparison.csv",index=False)
    if ledgers:pd.concat(ledgers,ignore_index=True).to_csv(OUT/"development_validation_ledgers.csv",index=False)
    allowed=table[table.qualifies]
    active=table[(table.development_trades+table.validation_trades)>0]
    pool=allowed if len(allowed) else active if len(active) else table
    selected=pool.sort_values(["score","strategy"],ascending=[False,True],kind="stable").iloc[0]
    rule=next(r for r in RULES if r["id"]==selected.strategy)
    frozen=dict(rule=rule,passed_development_validation=bool(selected.qualifies),selected_at_utc=pd.Timestamp.now(tz="UTC").isoformat(),
        reason="Largestminimumdevelopment/validationdailymean;lateroutcomesexcluded;diagnosticselectiononlyifnonepasses",warning=PLAN["warning"])
    (OUT/"frozen_selection.json").write_text(json.dumps(frozen,indent=2))
    results={};selected_ledgers=[];dailies=[]
    for name,days,sell_slip,delay in [("development",dev,1.,0),("validation",val,1.,0),("later_retrospective",later,1.,0),("additional_day",extra,1.,0),("later_sellslip2",later+extra,2.,0),("later_delay1m",later+extra,1.,1)]:
        t=replay(chosen,paths,rule,days,sell_slip,delay)
        results[name]=metrics(t,days)
        if len(t):t["period"]=name;selected_ledgers.append(t)
        if name in ["development","validation","later_retrospective","additional_day"]:
            d=base.daily_frame(t,days);d["period"]=name;dailies.append(d)
        print("FROZEN_EVALUATION",name,json.dumps(results[name],default=str),flush=True)
    summary=dict(selected_rule=rule,passed_development_validation=bool(selected.qualifies),candidate_count=3,
        results=results,period_days={"development":[str(x) for x in dev],"validation":[str(x) for x in val],"later_retrospective":[str(x) for x in later],"additional_day":[str(x) for x in extra]},
        raw_option_bars=len(bars),option_symbols=bars.symbol.nunique(),warning=PLAN["warning"])
    (OUT/"selected_summary.json").write_text(json.dumps(summary,indent=2,default=str))
    if selected_ledgers:pd.concat(selected_ledgers,ignore_index=True).to_csv(OUT/"selected_ledgers.csv",index=False)
    daily=pd.concat(dailies,ignore_index=True);daily.to_csv(OUT/"selected_daily.csv",index=False)
    fig,axs=plt.subplots(2,1,figsize=(13,8),sharex=True);x=np.arange(len(daily))
    axs[0].bar(x,daily.net_pnl,color=np.where(daily.net_pnl>=0,"#18865a","#d54b4b"));axs[0].set_ylabel("Daily modeled net P&L (INR)")
    axs[0].set_title("Exploratory structural strategy: "+rule["id"]+"; 5 fixed lots")
    axs[1].plot(x,daily.net_pnl.cumsum(),marker="o",markersize=3);axs[1].set_ylabel("Cumulative modeled net P&L (INR)")
    for ax in axs:
        ax.axhline(0,color="#555",linewidth=.8)
        for b in [len(dev)-.5,len(dev)+len(val)-.5,len(dev)+len(val)+len(later)-.5]:ax.axvline(b,color="#888",linestyle="--",linewidth=.7)
    axs[1].set_xticks(x,[str(v)[5:] for v in daily.date],rotation=45,ha="right")
    axs[1].set_xlabel("Development | validation | later retrospective dates | additional day")
    fig.tight_layout();fig.savefig(OUT/"selected_daily.png",dpi=180)
    print("FINAL",json.dumps(summary,default=str),flush=True)
if __name__=="__main__":main()

