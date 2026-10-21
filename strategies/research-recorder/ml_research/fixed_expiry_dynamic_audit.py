#!/usr/bin/env python3
"""Research-only fixed-expiry dynamic-strike audit. Never submits orders."""
from pathlib import Path
from datetime import time as dt_time
import sys, json, warnings
warnings.filterwarnings("ignore", category=FutureWarning)
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path("/home/ubuntu/openalgo")
RESEARCH = ROOT / "strategies/research-recorder/ml_research"
OUT = RESEARCH / "results"
sys.path.insert(0, str(RESEARCH))
import run_srsi_ml_research as core
EXPIRY = "06OCT26"
QUANTITY = 325
CUTOFF = dt_time(13, 30)
FIRST = pd.Timestamp("2026-09-01").date()
LAST = pd.Timestamp("2026-09-30").date()
PREFIX = "fixed_06oct26_dynamic_6otm_5lots_audit"

def charges(entry, exit_price):
    buy = entry * QUANTITY
    sell = exit_price * QUANTITY
    transaction = (buy + sell) * .0003553
    sebi = (buy + sell) * .000001
    return 40 + transaction + sebi + buy * .00003 + sell * .0015 + .18 * (40 + transaction + sebi)

def price_outcome(g, i):
    """Look only after entry, on the full held-contract price path."""
    entry = float(g.close.iloc[i])
    stop = entry - 15.0
    for j in range(i + 1, len(g)):
        row = g.iloc[j]
        px = None
        reason = None
        if float(row.low) < stop:
            px = float(row.open) if float(row.open) < stop else stop
            reason = "STOP"
        elif np.isfinite(row.k) and float(row.k) >= 80 and float(row.close) >= entry:
            px = float(row.close)
            reason = "SRSI_EXIT"
        elif row.effective_time.time() >= dt_time(15, 25):
            px = float(row.close)
            reason = "RESEARCH_EOD"
        if px is not None:
            path_times = g.effective_time.iloc[i:j+1]
            gaps = path_times.diff().dropna().dt.total_seconds() / 60
            return dict(entry_price=entry, exit_price=px, exit_time=row.effective_time,
                        reason=reason, gross_points=px-entry,
                        missing_minute_gaps=int((gaps > 1).sum()),
                        largest_gap_minutes=float(gaps.max()) if len(gaps) else 0.0,
                        exit_complete=True)
    return dict(entry_price=entry, exit_price=np.nan, exit_time=g.effective_time.iloc[-1],
                reason="UNRESOLVED_MISSING_EXIT_DATA", gross_points=np.nan,
                missing_minute_gaps=np.nan, largest_gap_minutes=np.nan, exit_complete=False)

def finish(d, mode):
    if d.empty:
        return d
    d = d.copy()
    d["mode"] = mode
    d["charges_rupees"] = d.apply(
        lambda r: charges(r.entry_price, r.exit_price) if pd.notna(r.exit_price) else np.nan,
        axis=1)
    d["gross_pnl_rupees"] = d.gross_points * QUANTITY
    d["slippage_rupees"] = QUANTITY
    d["net_pnl_rupees"] = d.gross_pnl_rupees - d.slippage_rupees - d.charges_rupees
    return d

def legacy_events(bars):
    rows = []
    for symbol, whole in bars.groupby("symbol", sort=True):
        for day, g0 in whole.groupby("session", sort=True):
            if day < FIRST or day > LAST:
                continue
            g = g0.sort_values("sample_time").copy()
            if g.spot.notna().any():
                g = g[g.spot.notna() & (g.spot_distance.abs() <= 300)].copy()
            if g.empty:
                continue
            g = g.reset_index(drop=True)
            zero = np.isfinite(g.k.to_numpy()) & (np.abs(g.k.to_numpy()) < 1e-10)
            prev = np.r_[False, zero[:-1]]
            for i in np.flatnonzero(zero & ~prev):
                row = g.iloc[i]
                if row.sample_time.time() >= CUTOFF:
                    continue
                signed = ((row.spot-row.strike) if row.option_type == "CE" else (row.strike-row.spot))/50.0
                if int(np.floor(signed+.5)) != -6:
                    continue
                outcome = core.trade_outcome(g, int(i), 1.0)
                if outcome is None:
                    continue
                rows.append(dict(symbol=symbol, session=day, sample_time=row.sample_time,
                                 option_type=row.option_type, strike=row.strike, expiry=row.expiry,
                                 premium=row.close, moneyness_signed_pct=row.moneyness_signed_pct,
                                 k=row.k, **outcome))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(core.replay(pd.DataFrame(rows), nearest=True))

def corrected_events(bars, spot_map):
    candidates = []
    lookup = {}
    coverage_rows = []
    for symbol, whole0 in bars.groupby("symbol", sort=True):
        whole = whole0.sort_values("timestamp").copy()
        whole["effective_time"] = whole.timestamp + pd.Timedelta(minutes=1)
        whole["exact_spot"] = whole.timestamp.map(spot_map)
        whole["atm"] = np.floor(whole.exact_spot / 50.0 + .5) * 50.0
        whole["target"] = np.where(whole.option_type.eq("CE"), whole.atm+300, whole.atm-300)
        prev_k = whole.k.shift(1)
        whole["zero_transition"] = (np.isfinite(whole.k) & np.isfinite(prev_k)
                                    & (np.abs(whole.k) < 1e-10)
                                    & (np.abs(prev_k) >= 1e-10))
        whole["eligible"] = whole.strike.eq(whole.target) & whole.exact_spot.notna()
        for day, g0 in whole.groupby("session", sort=True):
            if day < FIRST or day > LAST:
                continue
            g = g0.reset_index(drop=True)
            lookup[(symbol, day)] = g
            eligible = g[g.eligible]
            coverage_rows.append(dict(session=day, option_type=g.option_type.iloc[0],
                                      eligible_minutes=len(eligible),
                                      candidate_symbol=symbol))
            mask = g.zero_transition & g.eligible & (g.effective_time.dt.time < CUTOFF)
            for i in np.flatnonzero(mask.to_numpy()):
                row = g.iloc[i]
                candidates.append(dict(symbol=symbol, session=day,
                                       sample_time=row.effective_time, option_type=row.option_type,
                                       strike=int(row.strike), expiry=row.expiry,
                                       premium=float(row.close), exact_spot=float(row.exact_spot),
                                       k=float(row.k), row_i=int(i)))
    selected = []
    busy = {}
    for row in sorted(candidates, key=lambda x:(x["session"], x["sample_time"], x["option_type"])):
        key = (row["session"], row["option_type"])
        if key in busy and row["sample_time"] <= busy[key]:
            continue
        g = lookup[(row["symbol"], row["session"])]
        outcome = price_outcome(g, row["row_i"])
        busy[key] = outcome["exit_time"] if outcome["exit_complete"] else pd.Timestamp(row["session"]) + pd.Timedelta(days=1)
        selected.append({**row, **outcome})
    return pd.DataFrame(selected), pd.DataFrame(coverage_rows)

def main():
    cached = pd.read_parquet(OUT/"expiry_locked_6otm_5lots_bars.parquet")
    bars = cached[cached.expiry_code.eq(EXPIRY)].copy()
    print("FIXED_EXPIRY", EXPIRY, "symbols", bars.symbol.nunique(), "bars", len(bars), flush=True)
    _, spot = core.load_all()
    spot = spot.drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    spot_map = spot.set_index("timestamp")["spot"]
    legacy = finish(legacy_events(bars), "original_filtered_helper")
    corrected0, coverage = corrected_events(bars, spot_map)
    corrected = finish(corrected0, "corrected_full_contract_monitoring")
    ledger = pd.concat([legacy, corrected], ignore_index=True)
    daily_rows = []
    days = sorted(spot.timestamp.dt.date[(spot.timestamp.dt.date >= FIRST)&(spot.timestamp.dt.date <= LAST)].unique())
    for mode in ledger["mode"].unique():
        for day in days:
            t = ledger[(ledger["mode"] == mode)&(ledger.session == day)]
            daily_rows.append(dict(mode=mode, trade_date=day, trades=len(t),
                                   resolved_trades=int(t.net_pnl_rupees.notna().sum()),
                                   unresolved_trades=int(t.net_pnl_rupees.isna().sum()),
                                   gross_pnl_rupees=float(t.gross_pnl_rupees.sum()),
                                   net_pnl_rupees=float(t.net_pnl_rupees.sum())))
    daily = pd.DataFrame(daily_rows)
    ledger.to_csv(OUT/f"{PREFIX}_trades.csv", index=False)
    daily.to_csv(OUT/f"{PREFIX}_daily.csv", index=False)
    coverage.to_csv(OUT/f"{PREFIX}_coverage.csv", index=False)
    summary = daily.groupby("mode").agg(
        trades=("trades","sum"),resolved_trades=("resolved_trades","sum"),
        unresolved_trades=("unresolved_trades","sum"),
        gross_pnl_rupees=("gross_pnl_rupees","sum"),
        net_pnl_rupees=("net_pnl_rupees","sum"),
        winning_days=("net_pnl_rupees",lambda x:int((x>0).sum())),
        losing_days=("net_pnl_rupees",lambda x:int((x<0).sum()))).reset_index()
    summary.to_csv(OUT/f"{PREFIX}_summary.csv", index=False)
    comparison = daily.pivot(index="trade_date",columns="mode",values="net_pnl_rupees")
    fig, axes = plt.subplots(2,1,figsize=(13,8),sharex=True)
    x = np.arange(len(comparison))
    values = comparison["corrected_full_contract_monitoring"]
    axes[0].bar(x,values,color=np.where(values>=0,"#168354","#d34646"))
    axes[0].axhline(0,color="#555",linewidth=.8)
    axes[0].set_ylabel("Daily net P&L (INR)")
    axes[0].set_title("Fixed 06OCT26 expiry; dynamic ATM+300 CE / ATM-300 PE; 5 lots")
    for mode,label in [("corrected_full_contract_monitoring","Full held-contract price path"),
                       ("original_filtered_helper","Original helper: incomplete exit monitoring")]:
        axes[1].plot(x,comparison[mode].cumsum(),marker="o",markersize=3,label=label)
    axes[1].axhline(0,color="#555",linewidth=.8)
    axes[1].set_ylabel("Cumulative net P&L (INR)")
    axes[1].set_xticks(x,[str(v)[5:] for v in comparison.index],rotation=45,ha="right")
    axes[1].set_xlabel("September 2026 session")
    axes[1].legend()
    fig.tight_layout()
    fig.savefig(OUT/f"{PREFIX}.png",dpi=180)
    meta = dict(expiry=EXPIRY,lots=5,quantity=QUANTITY,candle_minutes=5,
                srsi=[4,4,2,2],entry="K transitions into zero",
                entry_cutoff="13:30",exit="K>=80 and price>=entry, or strict 15 point stop",
                end_of_day="15:25 research square off",sampling="1m close proxy",
                corrections=["full held-contract path retained","exact spot timestamp mapping",
                             "true previous symbol indicator sample","ATM rounded half up",
                             "close-derived signal known at next minute","unresolved trades retained"],
                previous_39252_and_98298_results="Use defective filtered exit helper; unvalidated.",
                gap_affected_corrected_trades=int((corrected.get("missing_minute_gaps",pd.Series(dtype=float))>0).sum()))
    (OUT/f"{PREFIX}_method.json").write_text(json.dumps(meta,indent=2))
    print("SUMMARY\n"+summary.to_string(index=False),flush=True)
    print("DAILY\n"+daily[daily["mode"].eq("corrected_full_contract_monitoring")].to_string(index=False),flush=True)
    print("EXITS",corrected.reason.value_counts().to_dict(),"GAP_AFFECTED",meta["gap_affected_corrected_trades"],flush=True)

if __name__ == "__main__":
    main()

