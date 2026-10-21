#!/usr/bin/env python3
"""Eight declared liquid-option strategy variants with causal chronology."""
from pathlib import Path
from datetime import time as dt_time
import json
import numpy as np,pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
OUT=Path("/home/ubuntu/openalgo/strategies/research-recorder/ml_research/results/liquid_strategy_study")
QTY=325
ENTRY_SLIP=.25
CUTOFF=dt_time(13,30)

def fee(entry,exit_price):
    buy=entry*QTY;sell=exit_price*QTY
    tx=(buy+sell)*.0003553;sebi=(buy+sell)*.000001
    return 40+tx+sebi+buy*.00003+sell*.0015+.18*(40+tx+sebi)

def setup(bars):
    pathcols=["timestamp","effective_time","open","high","low","close","k"]
    paths={}
    for key,g in bars.groupby(["symbol","session"],sort=False):
        paths[key]=g[pathcols].sort_values("timestamp").reset_index(drop=True)
    e=bars[bars.eligible & (bars.effective_time.dt.time<CUTOFF)].copy()
    e["premium_distance"]=(e.close-115).abs()
    # Select contract independent of its indicator signal or future P&L.
    chosen=e.sort_values(["session","effective_time","option_type","premium_distance","volume10","symbol"],
                        ascending=[True,True,True,True,False,True],kind="stable").drop_duplicates(
                        ["session","effective_time","option_type"],keep="first")
    return paths,chosen.sort_values(["session","effective_time","option_type"])

def signal_rows(chosen,rule,days):
    e=chosen[chosen.session.isin(days)]
    if rule["signal"]=="breakout":
        mask=np.where(e.option_type.eq("CE"),e.breakout_up.eq(True),e.breakout_down.eq(True))
    else:
        mask=e[f"signal_{rule['signal']}"].eq(True)
    if rule["trend"]:
        mask=mask & np.where(e.option_type.eq("CE"),e.trend_up.eq(True),e.trend_down.eq(True))
    return e[mask].sort_values(["effective_time","option_type"])

def outcome(row,path,rule,sell_slip=1.,delay=0):
    signal_time=row.effective_time
    entry_time=signal_time+pd.Timedelta(minutes=delay)
    starts=path.timestamp
    match=np.flatnonzero(starts.eq(entry_time).to_numpy())
    assumed_entry=not len(match)
    if len(match):
        entry_i=int(match[0]);base_entry=float(path.open.iloc[entry_i])
    else:
        known=path[path.effective_time<=entry_time]
        entry_i=int(np.searchsorted(starts.to_numpy(),np.datetime64(entry_time),side="left"))
        base_entry=float(known.close.iloc[-1])
    entry=base_entry+ENTRY_SLIP
    stop=entry-15;target=entry+30
    deadline=entry_time.normalize()+pd.Timedelta(hours=15,minutes=25)
    if rule["exit"]=="target":
        deadline=min(deadline,entry_time+pd.Timedelta(minutes=30))
    exit_px=None;exit_time=None;reason=None;source_i=None;assumed_exit=False
    for j in range(entry_i,len(path)):
        r=path.iloc[j]
        if r.effective_time>deadline:
            break
        if float(r.low)<stop:
            exit_px=float(r.open) if float(r.open)<stop else stop
            reason="STOP"
        elif rule["exit"]=="target" and float(r.high)>=target:
            exit_px=target;reason="TARGET"
        elif rule["exit"]=="srsi" and np.isfinite(r.k) and r.k>=80 and r.close>=entry:
            exit_px=float(r.close);reason="SRSI_EXIT"
        elif r.effective_time==deadline:
            exit_px=float(r.close);reason="TIME_LIMIT" if rule["exit"]=="target" else "EOD"
        if exit_px is not None:
            exit_time=r.effective_time;source_i=j
            break
    if exit_px is None:
        known=path[path.effective_time<=deadline]
        if known.empty:
            return dict(entry_time=entry_time,exit_time=deadline,exit_complete=False,reason="UNRESOLVED",net_pnl=np.nan)
        source_i=int(known.index[-1]);exit_px=float(known.close.iloc[-1]);exit_time=deadline
        reason="ASSUMED_DEADLINE_LAST_CLOSE";assumed_exit=True
    sell_fill=exit_px-sell_slip
    source_time=path.effective_time.iloc[source_i]
    stale=max(0.,(exit_time-source_time).total_seconds()/60)
    p=path.effective_time[(path.effective_time>=entry_time)&(path.effective_time<=source_time)]
    gaps=p.diff().dropna().dt.total_seconds()/60
    gap_count=int((gaps>1).sum())+int(stale>1)
    raw=(exit_px-base_entry)*QTY
    slip=(ENTRY_SLIP+sell_slip)*QTY
    costs=fee(entry,sell_fill)
    return dict(entry_time=entry_time,exit_time=exit_time,entry_base=base_entry,entry_price=entry,
                exit_quote_price=exit_px,exit_price=sell_fill,reason=reason,exit_complete=True,
                assumed_entry=assumed_entry,assumed_exit=assumed_exit,
                exit_price_available_time=source_time,exit_staleness_minutes=stale,
                missing_minute_gaps=gap_count,
                largest_gap_minutes=max(float(gaps.max()) if len(gaps) else 0.,stale),
                raw_gross_pnl=raw,slippage=slip,fees=costs,net_pnl=raw-slip-costs)

def replay(chosen,paths,rule,days,sell_slip=1.,delay=0):
    signals=signal_rows(chosen,rule,days)
    records=[];busy={};counts={};halts=set()
    for row in signals.itertuples(index=False):
        key=(row.session,row.option_type)
        if key in halts or counts.get(key,0)>=3:continue
        if key in busy and row.effective_time<=busy[key]+pd.Timedelta(minutes=5):continue
        # Exact symbol and expiry locked before reading any subsequent path.
        result=outcome(row,paths[(row.symbol,row.session)],rule,sell_slip,delay)
        busy[key]=result["exit_time"];counts[key]=counts.get(key,0)+1
        if not result["exit_complete"]:halts.add(key)
        records.append(dict(strategy=rule["id"],session=row.session,symbol=row.symbol,
                            option_type=row.option_type,strike=int(row.strike),expiry=row.expiry,
                            signal_time=row.effective_time,k=float(row.k),spot=float(row.spot),
                            premium=float(row.close),**result))
    ledger=pd.DataFrame(records)
    if not ledger.empty:
        for key,g in ledger.groupby(["session","option_type"]):
            g=g.sort_values("entry_time")
            if (g.entry_time.iloc[1:].to_numpy()<=g.exit_time.iloc[:-1].to_numpy()).any():
                raise AssertionError("Overlapping holdings")
        if not (ledger.exit_price_available_time<=ledger.exit_time).all():
            raise AssertionError("Future quote used in exit")
    return ledger

def daily_frame(ledger,days):
    rows=[]
    for day in days:
        t=ledger[ledger.session.eq(day)] if not ledger.empty else pd.DataFrame()
        rows.append(dict(date=day,trades=len(t),raw_gross_pnl=float(t.raw_gross_pnl.sum()) if len(t) else 0.,
                         net_pnl=float(t.net_pnl.sum()) if len(t) else 0.,
                         assumed_entries=int(t.assumed_entry.sum()) if len(t) else 0,
                         assumed_exits=int(t.assumed_exit.sum()) if len(t) else 0,
                         gap_affected_trades=int((t.missing_minute_gaps>0).sum()) if len(t) else 0))
    return pd.DataFrame(rows)

def metrics(ledger,days):
    d=daily_frame(ledger,days)
    cum=d.net_pnl.cumsum();dd=cum-cum.cummax().clip(lower=0)
    return dict(trades=len(ledger),days=len(days),raw_gross_pnl=float(d.raw_gross_pnl.sum()),
                net_pnl=float(d.net_pnl.sum()),mean_daily_net=float(d.net_pnl.mean()),
                max_daily_drawdown=float(-dd.min()),positive_days=int((d.net_pnl>0).sum()),
                negative_days=int((d.net_pnl<0).sum()),
                win_rate=float((ledger.net_pnl>0).mean()) if len(ledger) else None,
                assumed_entries=int(d.assumed_entries.sum()),assumed_exits=int(d.assumed_exits.sum()),
                gap_affected_trades=int(d.gap_affected_trades.sum()),
                max_exit_staleness_minutes=float(ledger.exit_staleness_minutes.max()) if len(ledger) else 0.,
                unresolved=int((~ledger.exit_complete).sum()) if len(ledger) else 0)

def main():
    plan=json.loads((OUT/"declared_plan.json").read_text())
    bars=pd.read_parquet(OUT/"causal_features.parquet")
    print("ENGINE",len(bars),"bars",flush=True)
    paths,chosen=setup(bars)
    all_days=sorted({x for x in chosen.session if pd.Timestamp("2026-09-01").date()<=x<=pd.Timestamp("2026-09-24").date()})
    # Common recorded study days are not chosen based on strategy outcomes.
    dev=[d for d in all_days if d<=pd.Timestamp("2026-09-11").date()]
    val=[d for d in all_days if pd.Timestamp("2026-09-15").date()<=d<=pd.Timestamp("2026-09-18").date()]
    evaluation=[d for d in all_days if pd.Timestamp("2026-09-21").date()<=d<=pd.Timestamp("2026-09-24").date()]
    extra=[pd.Timestamp("2026-10-01").date()] if pd.Timestamp("2026-10-01").date() in chosen.session.unique() else []
    comparisons=[];ledgers=[]
    for rule in plan["hypotheses"]:
        a=replay(chosen,paths,rule,dev);b=replay(chosen,paths,rule,val)
        ma=metrics(a,dev);mb=metrics(b,val)
        qualifies=(ma["net_pnl"]>0 and mb["net_pnl"]>0 and ma["trades"]>=10 and mb["trades"]>=5 and
                   ma["unresolved"]==0 and mb["unresolved"]==0 and
                   ma["max_exit_staleness_minutes"]<=2 and mb["max_exit_staleness_minutes"]<=2)
        row=dict(strategy=rule["id"],qualifies=qualifies,
                 score=min(ma["mean_daily_net"],mb["mean_daily_net"]),
                 **{"development_"+k:v for k,v in ma.items()},
                 **{"validation_"+k:v for k,v in mb.items()})
        comparisons.append(row)
        if len(a):a["period"]="development";ledgers.append(a)
        if len(b):b["period"]="validation";ledgers.append(b)
        print("CANDIDATE",rule["id"],"dev_net",round(ma["net_pnl"],2),"val_net",round(mb["net_pnl"],2),
              "trades",ma["trades"],mb["trades"],"qualifies",qualifies,flush=True)
    table=pd.DataFrame(comparisons)
    allowed=table[table.qualifies]
    pool=allowed if len(allowed) else table
    selected=pool.sort_values(["score","strategy"],ascending=[False,True],kind="stable").iloc[0]
    rule=next(r for r in plan["hypotheses"] if r["id"]==selected.strategy)
    frozen=dict(rule=rule,selected_at_utc=pd.Timestamp.now(tz="UTC").isoformat(),
                passed_development_validation=bool(selected.qualifies),
                reason="Best minimum development/validation daily net; later outcomes excluded from selection",
                selection_table=table.to_dict("records"),
                warning="No candidate passed" if not len(allowed) else "Small retrospective sample; not proven live profitability")
    # Freeze selection before processing evaluation and additional-day outcomes.
    (OUT/"frozen_selection.json").write_text(json.dumps(frozen,indent=2,default=str))
    table.to_csv(OUT/"development_validation_comparison.csv",index=False)
    if ledgers:pd.concat(ledgers,ignore_index=True).to_csv(OUT/"development_validation_ledgers.csv",index=False)
    results={}
    selected_ledgers=[];daily=[]
    for name,days,sell_slip,delay in [
            ("development",dev,1.,0),("validation",val,1.,0),
            ("retrospective_evaluation",evaluation,1.,0),("additional_day",extra,1.,0),
            ("evaluation_slippage2",evaluation+extra,2.,0),
            ("evaluation_delay1m",evaluation+extra,1.,1)]:
        t=replay(chosen,paths,rule,days,sell_slip,delay)
        results[name]=metrics(t,days) if days else dict(trades=0,reason="No additional data")
        if len(t):t["period"]=name;selected_ledgers.append(t)
        if name in ["development","validation","retrospective_evaluation","additional_day"]:
            d=daily_frame(t,days);d["period"]=name;daily.append(d)
        print("FROZEN_EVALUATION",name,json.dumps(results[name],default=str),flush=True)
    summary=dict(selected_rule=rule,passed_development_validation=bool(selected.qualifies),
                 candidate_count=len(plan["hypotheses"]),results=results,
                 study_days=[str(x) for x in all_days],additional_days=[str(x) for x in extra],
                 method_limits=plan["warning"])
    (OUT/"selected_summary.json").write_text(json.dumps(summary,indent=2,default=str))
    if selected_ledgers:pd.concat(selected_ledgers,ignore_index=True).to_csv(OUT/"selected_ledgers.csv",index=False)
    d=pd.concat(daily,ignore_index=True);d.to_csv(OUT/"selected_daily.csv",index=False)
    fig,axes=plt.subplots(2,1,figsize=(13,8),sharex=True)
    x=np.arange(len(d));colors=np.where(d.net_pnl>=0,"#168354","#d34646")
    axes[0].bar(x,d.net_pnl,color=colors);axes[0].axhline(0,color="#555",linewidth=.8)
    axes[0].set_title("Frozen exploratory candidate: "+rule["id"]+"; five fixed lots")
    axes[0].set_ylabel("Daily modeled net P&L (INR)")
    axes[1].plot(x,d.net_pnl.cumsum(),marker="o",markersize=3)
    axes[1].axhline(0,color="#555",linewidth=.8);axes[1].set_ylabel("Cumulative modeled net P&L (INR)")
    for boundary in [len(dev)-.5,len(dev)+len(val)-.5,len(all_days)-.5]:
        for ax in axes:ax.axvline(boundary,color="#888",linestyle="--",linewidth=.7)
    axes[1].set_xticks(x,[str(v)[5:] for v in d.date],rotation=45,ha="right")
    axes[1].set_xlabel("Development | validation | later retrospective dates | additional day")
    fig.tight_layout();fig.savefig(OUT/"selected_daily.png",dpi=180)
    print("FINAL",json.dumps(summary,default=str),flush=True)
if __name__=="__main__":main()

