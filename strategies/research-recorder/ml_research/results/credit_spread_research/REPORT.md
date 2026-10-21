# Defined-risk NIFTY credit-spread study — 2 October 2026

Research-only results. No orders were placed and no live strategy settings were changed.

## Predeclared method

Two variants were specified before computing their outcomes:

1. Trend only: NIFTY above its latest completed five-minute EMA20 with rising EMA -> short PE; below EMA20 with falling EMA -> short CE.
2. Same trend rule plus short-option provisional five-minute SRSI K crossing below 80 from >=80 on the preceding observed minute.

Expiry was fixed at 29SEP26. The short strike was exactly rounded ATM +300 for CE / -300 for PE. The protective long option was another 100 points OTM, with the same expiry and option type. Quantity was fixed at five lots (325 units) per leg.

Entries used 10:00 inclusive–13:00 exclusive signals and the next minute's paired opens. Both legs needed at least nine positive-volume observations in the preceding ten actual minutes, including the latest completed minute. One spread could be open; maximum two attempts per day.

Exits used synchronized minute close observations, at 50% of initial credit profit or 100% of initial credit loss, otherwise after 30 elapsed minutes. Independent leg highs and lows were never subtracted to invent spread OHLC. Four points of adverse round-trip execution allowance were charged (one point per leg on entry and exit) plus four-order brokerage, transaction charges, stamp duty, STT, SEBI and GST.

## Selection and evaluation

Development training: Sep1–11, nine sessions.
Validation: Sep15–18, four sessions.
The trend-only variant was frozen before evaluating Sep21–24 because it had the better validation subtotal. Its validation was negative, so this is an evaluation benchmark, not an approved strategy.

These dates have appeared in earlier research; this is a retrospective temporal evaluation rather than a pristine untouched holdout.

| Variant | Phase | Resolved spreads | Unresolved attempts | Raw gross | Gross after execution allowance | Estimated fees | Net resolved subtotal |
|---|---|---:|---:|---:|---:|---:|---:|
| Trend only | Training | 17 | 1 | -1,007.50 | -23,107.50 | 4,705.77 | -27,813.27 |
| Trend only | Validation | 8 | 0 | +1,121.25 | -9,278.75 | 1,780.49 | -11,059.24 |
| Trend + SRSI reversal | Training | 12 | 4 | -2,112.50 | -17,712.50 | 3,656.37 | -21,368.87 |
| Trend + SRSI reversal | Validation | 7 | 0 | -2,681.25 | -11,781.25 | 1,514.19 | -13,295.44 |
| Trend only, selected benchmark | Sep21–24 test | 8 | 0 | +292.50 | -10,107.50 | 1,074.63 | -11,182.13 |

Test daily net: Sep21 -3,271.11; Sep22 -2,840.40; Sep23 -2,160.72; Sep24 -2,909.90.

## Integrity and constraints

There were 651,661 usable one-minute bars across 258 archived 29SEP26 contracts. Actual spot timestamps were joined directly; no spot forward/backfill across symbols was used.

Missing paired entry and exit observations were retained explicitly as unresolved records. Development phase totals are resolved subtotals and cannot be claimed as full portfolio results. Training trend-only had one unresolved exit. The SRSI variant had three unresolved exits and one missing next-minute entry.

Seven of 25 resolved trend-only development spreads had at least one missing paired observation while held. Four of 19 resolved SRSI development spreads did. The selected eight-spread test had zero missing paired minutes, and all eight exited at the fixed 30-minute time limit. No resolved entry or exit spread mark violated the 0–100 width bounds. Round-trip execution allowance was exactly four points on all resolved spreads.

This remains a one-minute close/open proxy. Paired minute closes need not be exchange-simultaneous last trades; no bid/ask tape or five-second live fill window can be reconstructed. EMA used only fully observed completed five-minute spot bars. SRSI used the existing OpenAlgo-equivalent provisional-close indicator helper, not the defective event/outcome helpers.

## Conclusion

Neither tested credit-spread variant established a useful edge. The selected test's raw gross of +292.50 across eight spreads is negligible before execution friction; net is negative even before fees once the four-point execution allowance is included. Do not deploy either variant based on this sample, increase lots, or search these same test dates for a favorable parameter combination.

## Artifacts

- Script: ../credit_spread_research.py relative to results parent, or absolute /home/ubuntu/openalgo/strategies/research-recorder/ml_research/credit_spread_research.py
- method.json
- selection_before_test.json
- summary.csv
- ema20_trend_development_trades.csv and development_daily.csv
- ema20_trend_srsi_reversal_development_trades.csv and development_daily.csv
- selected_test_trades.csv
- selected_test_daily.csv

