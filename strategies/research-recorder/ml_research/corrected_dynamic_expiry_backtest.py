#!/usr/bin/env python3
"""Corrected research-only dynamic strike/expiry backtest. No order APIs."""
from pathlib import Path
from datetime import time as dt_time
import json, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RESEARCH = Path("/home/ubuntu/openalgo/strategies/research-recorder/ml_research")
OUT = RESEARCH / "results"
sys.path.insert(0, str(RESEARCH))
import fixed_expiry_dynamic_audit as fixed

FIRST = pd.Timestamp("2026-09-01").date()
LAST = pd.Timestamp("2026-09-30").date()
CUTOFF = dt_time(13,30)
MODEL_EOD = "--model-missing-eod" in sys.argv
PREFIX = "corrected_dynamic_6otm_5lots_september_2026" + ("_modeled_eod" if MODEL_EOD else "")
EXPIRIES = ("06OCT26","13OCT26","19OCT26","27OCT26","03NOV26","23NOV26","29DEC26")

def load_spot():
    # Read spot directly, without session ffill/bfill or option-symbol ordering.
    import duckdb
    conn = duckdb.connect("/home/ubuntu/openalgo/db/historify.duckdb", read_only=True)
    spot = conn.execute(
        "SELECT timestamp,close FROM market_data WHERE symbol='NIFTY' "
        "AND exchange='NSE_INDEX' AND interval='1m'").fetchdf()
    conn.close()
    spot["timestamp"] = pd.to_datetime(spot.timestamp,unit="s",utc=True).dt.tz_convert(
        "Asia/Kolkata").dt.tz_localize(None)
    spot["spot"] = pd.to_numeric(spot.close,errors="coerce")
    return spot.dropna(subset=["spot"]).drop_duplicates("timestamp",keep="last").sort_values("timestamp")

def prepare(bars, spot):
    candidates = []
    paths = {}
    coverage = []
    spot_map = spot.set_index("timestamp").spot
    for index,(symbol,raw) in enumerate(bars.groupby("symbol",sort=True),1):
        whole = raw.sort_values("timestamp").copy().reset_index(drop=True)
        whole["effective_time"] = whole.timestamp + pd.Timedelta(minutes=1)
        whole["exact_spot"] = whole.timestamp.map(spot_map)
        whole["atm"] = np.floor(whole.exact_spot/50+.5)*50
        whole["target_strike"] = np.where(whole.option_type.eq("CE"),whole.atm+300,whole.atm-300)
        prev = whole.k.shift(1)
        whole["zero_transition"] = (np.isfinite(whole.k)&np.isfinite(prev)&
                                   (np.abs(whole.k)<1e-10)&(np.abs(prev)>=1e-10))
        # No entry history is filtered before indicator transitions or exits.
        whole["eligible"] = (whole.strike.eq(whole.target_strike)&whole.exact_spot.notna()&
                             (whole.expiry>=whole.session))
        test = whole[(whole.session>=FIRST)&(whole.session<=LAST)]
        for day,g0 in test.groupby("session",sort=True):
            g = g0.copy().reset_index(drop=True)
            paths[(symbol,day)] = g
            before_cutoff = g.effective_time.dt.time < CUTOFF
            coverage.append(dict(session=day,expiry_code=g.expiry_code.iloc[0],
                                 option_type=g.option_type.iloc[0],symbol=symbol,
                                 total_minutes=len(g),eligible_entry_minutes=int((g.eligible&before_cutoff).sum()),
                                 first=g.effective_time.iloc[0],last=g.effective_time.iloc[-1]))
            mask = g.eligible & g.zero_transition & before_cutoff
            for i in np.flatnonzero(mask.to_numpy()):
                r = g.iloc[i]
                candidates.append(dict(symbol=symbol,session=day,sample_time=r.effective_time,
                                       option_type=r.option_type,strike=int(r.strike),
                                       expiry=r.expiry,expiry_code=r.expiry_code,
                                       premium=float(r.close),exact_spot=float(r.exact_spot),
                                       atm=int(r.atm),k=float(r.k),row_i=int(i)))
        if index % 100 == 0:
            print("PREPARED",index,"symbols;",len(candidates),"candidate signals",flush=True)
    return pd.DataFrame(candidates),paths,pd.DataFrame(coverage)

def outcome_for(g,i):
    if not MODEL_EOD:
        return fixed.price_outcome(g,i)
    entry=float(g.close.iloc[i]);stop=entry-15.
    eod=g.effective_time.iloc[i].normalize()+pd.Timedelta(hours=15,minutes=25)
    exit_row=None;reason=None;px=None;exit_time=None;assumed=False
    for j in range(i+1,len(g)):
        r=g.iloc[j]
        if r.effective_time>eod:
            break
        if float(r.low)<stop:
            px=float(r.open) if float(r.open)<stop else stop
            reason="STOP"
        elif np.isfinite(r.k) and float(r.k)>=80 and float(r.close)>=entry:
            px=float(r.close);reason="SRSI_EXIT"
        elif r.effective_time==eod:
            px=float(r.close);reason="RESEARCH_EOD"
        if reason is not None:
            exit_row=j;exit_time=r.effective_time
            break
    if exit_row is None:
        available=g[(g.index>=i)&(g.effective_time<=eod)]
        exit_row=int(available.index[-1])
        px=float(g.close.iloc[exit_row])
        exit_time=eod
        reason="ASSUMED_EOD_LAST_OBSERVED"
        assumed=True
    source=g.iloc[exit_row]
    gaps=g.effective_time.iloc[i:exit_row+1].diff().dropna().dt.total_seconds()/60
    stale=max(0.,(exit_time-source.effective_time).total_seconds()/60)
    missing=int((gaps>1).sum())+int(stale>1)
    largest=max(float(gaps.max()) if len(gaps) else 0.,stale)
    return dict(entry_price=entry,exit_price=px,exit_time=exit_time,reason=reason,
                gross_points=px-entry,missing_minute_gaps=missing,
                largest_gap_minutes=largest,exit_complete=True,
                assumed_eod_exit=assumed,exit_price_candle_start=source.timestamp,
                exit_price_available_time=source.effective_time,
                exit_price_staleness_minutes=stale)

def replay(candidates,paths):
    selected = []
    # Lock one bought contract per side. An unresolved contract blocks later
    # entries on that side for the remainder of the entire research period.
    busy = {}
    unresolved_sides = set()
    skipped_busy = 0
    ties = 0
    for (day,ts,side),group in candidates.groupby(["session","sample_time","option_type"],sort=True):
        if side in unresolved_sides or (side in busy and ts<=busy[side]):
            skipped_busy += len(group)
            continue
        eligible = group.sort_values(["premium","expiry","symbol"],kind="stable")
        row = eligible.iloc[0].to_dict()
        ties += int(len(group)>1)
        result = outcome_for(paths[(row["symbol"],day)],int(row["row_i"]))
        busy[side] = result["exit_time"]
        if not result["exit_complete"]:
            unresolved_sides.add(side)
        row["simultaneous_signal_expiries"] = len(group)
        selected.append({**row,**result})
    ledger = fixed.finish(pd.DataFrame(selected),"corrected_dynamic_expiry_modeled_eod" if MODEL_EOD else "corrected_dynamic_expiry_strict")
    return ledger,dict(candidate_signals=len(candidates),blocked_candidate_signals=skipped_busy,
                       entry_minutes_with_multiple_signalling_expiries=ties,
                       unresolved_sides=sorted(unresolved_sides))

def validate(candidates,ledger,paths):
    if ledger.empty:
        raise RuntimeError("No selected trades; cannot report P&L")
    for side,g in ledger.groupby("option_type"):
        g=g.sort_values("sample_time")
        if (g.sample_time.iloc[1:].to_numpy()<=g.exit_time.iloc[:-1].to_numpy()).any():
            raise AssertionError(f"Overlapping {side} holdings")
    for r in ledger.itertuples():
        if r.strike != r.atm+(300 if r.option_type=="CE" else -300):
            raise AssertionError("Entry strike was not current ATM +/-300")
        if r.expiry < r.session or r.sample_time.time()>=CUTOFF:
            raise AssertionError("Inactive expiry or late entry")
        g=paths[(r.symbol,r.session)]
        q=g.iloc[r.row_i]
        if q.effective_time!=r.sample_time or not q.zero_transition:
            raise AssertionError("Incorrect zero-transition/timestamp")
        if r.exit_complete:
            expected=outcome_for(g,r.row_i)
            if expected["exit_time"]!=r.exit_time or abs(expected["exit_price"]-r.exit_price)>1e-9:
                raise AssertionError("Exit does not match full held contract")
    print("VALIDATED",len(ledger),"trade strike, expiry, timing, lock and exit checks",flush=True)

def main():
    cached=pd.read_parquet(OUT/"expiry_locked_6otm_5lots_bars.parquet")
    bars=cached[cached.expiry_code.isin(EXPIRIES)].copy()
    spot=load_spot()
    candidates,paths,coverage=prepare(bars,spot)
    ledger,stats=replay(candidates,paths)
    validate(candidates,ledger,paths)
    days=sorted(spot.timestamp.dt.date[(spot.timestamp.dt.date>=FIRST)&(spot.timestamp.dt.date<=LAST)].unique())
    rows=[]
    for day in days:
        t=ledger[ledger.session.eq(day)]
        rows.append(dict(trade_date=day,trades=len(t),
                         resolved_trades=int(t.exit_complete.sum()),
                         unresolved_trades=int((~t.exit_complete).sum()),
                         gross_pnl_rupees=float(t.gross_pnl_rupees.sum()),
                         slippage_rupees=float(t.slippage_rupees[t.exit_complete].sum()),
                         charges_rupees=float(t.charges_rupees.sum()),
                         net_pnl_rupees=float(t.net_pnl_rupees.sum()),
                         gap_affected_trades=int((t.missing_minute_gaps>0).sum())))
    daily=pd.DataFrame(rows)
    by_expiry=ledger.groupby("expiry_code").agg(trades=("symbol","size"),
                  resolved_trades=("exit_complete","sum"),gross_pnl_rupees=("gross_pnl_rupees","sum"),
                  net_pnl_rupees=("net_pnl_rupees","sum"),
                  gap_affected_trades=("missing_minute_gaps",lambda s:int((s>0).sum()))).reset_index()
    by_side=ledger.groupby("option_type").agg(trades=("symbol","size"),
                  gross_pnl_rupees=("gross_pnl_rupees","sum"),net_pnl_rupees=("net_pnl_rupees","sum")).reset_index()
    # Keep invalid original results as a diagnostic reference only.
    old=pd.read_csv(OUT/"legacy_dynamic_active_expiries_before_1330_5lots_daily.csv")
    old_date="trade_date" if "trade_date" in old else "session"
    old[old_date]=pd.to_datetime(old[old_date]).dt.date
    old_daily=old.groupby(old_date).net_pnl_rupees.sum().reindex(days,fill_value=0)
    summary=dict(**stats,symbols=int(bars.symbol.nunique()),minute_bars=int(len(bars)),
                 expiries=list(EXPIRIES),trades=len(ledger),
                 resolved_trades=int(ledger.exit_complete.sum()),
                 unresolved_trades=int((~ledger.exit_complete).sum()),
                 gross_pnl_rupees=float(ledger.gross_pnl_rupees.sum()),
                 sell_slippage_rupees=float(ledger.slippage_rupees[ledger.exit_complete].sum()),
                 charges_rupees=float(ledger.charges_rupees.sum()),
                 net_pnl_rupees=float(ledger.net_pnl_rupees.sum()),
                 positive_days=int((daily.net_pnl_rupees>0).sum()),
                 negative_days=int((daily.net_pnl_rupees<0).sum()),
                 zero_days=int((daily.net_pnl_rupees==0).sum()),
                 gap_affected_trades=int((ledger.missing_minute_gaps>0).sum()),
                 exit_reasons=ledger.reason.value_counts().to_dict(),
                 original_invalid_net_pnl_rupees=float(old_daily.sum()))
    if MODEL_EOD:
        modeled=ledger[ledger.assumed_eod_exit]
        summary.update(assumed_eod_exits=len(modeled),
                       median_assumed_exit_staleness_minutes=float(modeled.exit_price_staleness_minutes.median()) if len(modeled) else 0.,
                       max_assumed_exit_staleness_minutes=float(modeled.exit_price_staleness_minutes.max()) if len(modeled) else 0.,
                       raw_gross_after_slippage_rupees=summary["gross_pnl_rupees"]-summary["sell_slippage_rupees"])
    method=dict(period=["2026-09-01","2026-09-30"],lots=5,quantity=325,
                candle_minutes=5,srsi=[4,4,2,2],
                entry="K transitions from finite nonzero to zero; exact current rounded ATM+300 CE / ATM-300 PE",
                expiry_selection="First qualifying zero-transition signal across the seven available active expiries; simultaneous signals: lowest premium, then earliest expiry, then symbol",
                hold="Lock exact contract and monitor all available subsequent held-contract bars; never filter exits by spot distance",
                max_holdings="One CE and one PE; independent; unresolved side blocks all future entries",
                cutoff="13:30; candle-start timestamps become available one minute later",
                exit="Strict 15-point stop, or K>=80 at price>=entry, or 15:25 research square-off",
                premium_band="Unrestricted positive premium, as original dynamic test",
                fees="Same prior proxy, quantity325 and STT0.15%; 1-point sell slippage separately",
                limitations=["1m OHLCV proxy, no actual 1sec/tick data or five-second limit-fill simulation",
                             "Sparse candles can conceal intervening stop/indicator events",
                             "Stop within a minute is assumed before the minute-close SRSI signal",
                             "No backward/forward spot fill for entries",
                             "No inferred/reconstructed prices for missing bars",
                             "P&L is realized subset only if unresolved_trades>0",
                             "Cached indicator calculation finalizes the last available sampled close of each interval"],
                cache="expiry_locked_6otm_5lots_bars.parquet",
                legacy_profit="Original +98298 result is invalid; diagnostic comparison only",
                daily_exit_model="15:25 at latest close already known by then; missing quotes flagged as ASSUMED_EOD_LAST_OBSERVED" if MODEL_EOD else "Missing daily exit remains unresolved; never dropped",
                full_month_result_status="Assumption-dependent estimate with explicitly modeled exits" if MODEL_EOD else "Incomplete if missing exit blocks a side")
    for name,data in (("trades",ledger),("daily",daily),("by_expiry",by_expiry),
                      ("by_side",by_side),("coverage",coverage),("candidates",candidates)):
        data.to_csv(OUT/f"{PREFIX}_{name}.csv",index=False)
    (OUT/f"{PREFIX}_summary.json").write_text(json.dumps(summary,indent=2,default=str))
    (OUT/f"{PREFIX}_method.json").write_text(json.dumps(method,indent=2,default=str))
    fig,ax=plt.subplots(2,1,figsize=(13,8),sharex=True)
    x=np.arange(len(days));v=daily.net_pnl_rupees
    ax[0].bar(x,v,color=np.where(v>=0,"#168354","#d34646"))
    ax[0].axhline(0,color="#555",linewidth=.8)
    ax[0].set_title("Dynamic ATM+300 CE / ATM-300 PE; seven monitored expiries; 5 lots")
    ax[0].set_ylabel("Daily estimated net P&L (INR)")
    ax[1].plot(x,v.cumsum(),marker="o",markersize=3,label="Corrected estimate; flagged stale prices at missing daily exits" if MODEL_EOD else "Strict realised subset; missing exits unresolved")
    ax[1].plot(x,old_daily.cumsum(),marker="o",markersize=3,linestyle="--",label="INVALID original simulation: skipped prices/signals")
    ax[1].axhline(0,color="#555",linewidth=.8)
    ax[1].set_ylabel("Cumulative estimated net P&L (INR)")
    ax[1].set_xticks(x,[str(day)[5:] for day in days],rotation=45,ha="right")
    ax[1].set_xlabel("September 2026 session")
    ax[1].legend()
    fig.tight_layout()
    fig.savefig(OUT/f"{PREFIX}.png",dpi=180)
    print("SUMMARY",json.dumps(summary,default=str),flush=True)
    print("DAILY\n"+daily.to_string(index=False),flush=True)
    print("BY_EXPIRY\n"+by_expiry.to_string(index=False),flush=True)
    print("BY_SIDE\n"+by_side.to_string(index=False),flush=True)

if __name__=="__main__":
    main()

