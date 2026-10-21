# NIFTY SRSI research conclusion — 1 October 2026

Research only. The live strategy, its configuration, schedules, and orders were not changed.

## Data used

- 733,320 positive one-minute NIFTY option bars across 303 symbols, 24 August to 30 September 2026.
- Existing event replays use 1-minute bars to simulate provisional values inside 1-, 5-, and 15-minute candles.
- Chronological split: train through 16 September (11 active days); validation 17–22 September (4 days); test 23–30 September (6 days).
- The archive has no tick stream. It cannot prove a five-second limit-buy fill, bid/ask spread, partial fill, or intrabar stop/exit ordering.

## Rules tested

- Live-equivalent SRSI: RSI length 4, StochRSI length 4, K/D smoothing 2, entry K=0, exit K>=80 while at or above entry, 15-point stop.
- One open position per CE/PE leg; 5-minute candle setting.
- Costs: fee/tax proxy plus 1.00 point adverse sell slippage per trade.

## Findings

- 1-, 5-, and 15-minute nearest-ATM versions are negative on held-out test data after costs.
- Exact six-strikes OTM is the only strike distance that came close to survival. Its unrestricted 5-minute outcome is still negative after costs on validation and test.
- Time cutoff is the only simple filter with a positive result in all three periods: no new entry at or after 13:30.
- The ₹100–₹120 premium result has only one validation trade, so it is not usable evidence.
- Option-volume thresholds and 15-minute spot direction did not hold up consistently.

## Candidate for a frozen paper test

`5-minute / exact 6-OTM / K=0 / no new entries after 13:30 / fixed 1 lot`

This is a paper-only candidate, not a live recommendation. Daily outcomes after costs and 1-point sell slippage:

| Split | Active days | Trades | Net points | Positive days | Worst day |
|---|---:|---:|---:|---:|---:|
| test | 6 | 31 | 15.44 | 4 | -2.32 |
| train | 11 | 62 | 31.02 | 8 | -29.86 |
| validation | 4 | 24 | 6.33 | 2 | -5.76 |

The full daily record is `exact_6otm_before_1330_daily.csv`.

## Model decision

Do not deploy ML or deep learning now. The existing logistic model and two-layer neural network did not beat the baseline out of sample. This sample is too short, has incomplete expiry coverage, and has too few independent trading days for a reliable strike-selection model. A more complex model would overfit rather than create an edge.

## Next research step

Run the frozen paper candidate through several expiry cycles while saving raw WebSocket ticks, current-candle SRSI values, limit-order submission/acknowledgement/cancel/fill timestamps, and exits. Re-run the test only after at least 40–60 independent trading days. Keep lots fixed; do not add loss recovery or Martingale sizing.
