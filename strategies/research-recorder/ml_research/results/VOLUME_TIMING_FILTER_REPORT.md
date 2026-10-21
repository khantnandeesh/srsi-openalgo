# 5-minute SRSI volume, time, strike, and premium filter study

Research only. No strategy configuration, scheduler, or live orders were changed.

## Method

- Existing 5-minute SRSI event set: RSI length 4, StochRSI length 4, K/D 2, entry K=0, exit K>=80 or 15-point stop.
- One position per CE/PE at a time. Net result subtracts a fee/tax proxy and 1.00 point adverse sell slippage.
- `exact_6_otm` means exactly six 50-point strikes OTM, reconstructed from each event’s original saved moneyness field.
- Policies were ranked only on chronological training data; validation and test were not used for selection.

## Train-only selected policy

`exact_6_otm_countertrend_15m` was top on training after requiring at least 25 trades across 5 days. It is not a live recommendation.

| Period | Trades | Days | Gross points | Net after fees + 1pt sell slippage | Win rate |
|---|---:|---:|---:|---:|---:|
| train | 46 | 11 | 141.50 | 47.17 | 54.3% |
| validation | 29 | 4 | 21.50 | -32.60 | 41.4% |
| test | 34 | 6 | 56.70 | -5.89 | 47.1% |

## Decision

The exact six-OTM baseline remains negative after conservative costs on validation and test. Volume is worth recording, but the current sample does not support a volume gate. Do not train or deploy an ML/DL strike selector: the prior logistic model and small neural net failed out of sample, and this filter sweep creates additional selection risk.

Next: freeze one paper-only 5-minute rule and record raw WebSocket ticks, indicator values, submitted limit orders, fills/cancels, and exits for several expiry cycles. One-minute OHLCV cannot validate a five-second limit fill, spread, or intrabar exit ordering.
