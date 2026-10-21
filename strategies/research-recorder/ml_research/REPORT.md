# NIFTY SRSI strike and ML research

**Status: research only. No orders sent. No live config or strategy process changed.**

## Recommendation

- Keep **5-minute SRSI** as the leading interval for further research. It was the least-bad baseline on the later dates, but still lost before brokerage and other estimated charges.
- Do **not** add the logistic model or neural network to the live strategy. Neither produced positive results on the test dates; neither demonstrated a reliable predictive advantage.
- A dynamically selected option about **6 strikes (300 NIFTY points) OTM** was the most interesting strike hypothesis in this sample. It was close to flat after estimated fees and a 1-point sell-slippage stress, and the result was selected after comparing many strike buckets. Use it only as a preregistered paper-trading hypothesis after collecting more 1-second data.
- The premium band **₹50–₹100 by itself did not generalize**. It was positive before fees on the test dates but lost on validation dates and after modeled costs.
- The active VM configuration currently selects **SENSEX**, five lots per leg. This report studies **NIFTY** because that is where archived minute data exists. It is not evidence for changing the SENSEX workers.

## Data and split

The run read data on the VM from:

1. /home/ubuntu/openalgo/strategies/scripts/historical_option_data/NIFTY/29SEP26/1m/
2. /home/ubuntu/openalgo/db/historify.duckdb, Historify's NIFTY 1-minute records

After removing zero/missing quote placeholders, the combined input had **733,320 positive option-minute bars over 303 symbols**, from 24 August through 30 September 2026. The archive included the 22 Sep expiry (one CE contract), 29 Sep (258 CE/PE contracts, through 24 Sep), and 6 Oct / 13 Oct (44 CE/PE contracts, uneven starts). There were **7,640 NIFTY index minute bars** for spot/moneyness and market-regime features.

Splits were chronological and grouped by date:

- Train: through 16 Sep
- Validation: 17–22 Sep
- Test: 23–30 Sep

The test period had six active dates in the selected trade replay; validation had four. Several contract histories are sparse, and 24 Sep's older 29 Sep-expiry archive is incomplete. These short periods do not establish a durable edge.

## Strategy reconstructed for research

- Indicator: StochRSI RSI=4, lookback=4, K=2, D=2, 0–100 scale. Candle state is anchored to 09:15 and carries through sessions.
- Provisional candle: each historical 1-minute close is used as the current close inside the still-forming 1/5/15-minute candle. A custom incremental calculation was compared with the VM's OpenAlgo SDK on a sample; K/D matched exactly.
- Entry: first observed sample in a contiguous run where K equals 0, before 15:15, within 300 NIFTY points of spot. One-minute data only observes one point per minute, not the actual first second.
- Exit: stop if an observed minute low goes strictly below entry−15; otherwise exit at an observed price when K≥80 and price is at least entry. Stop wins if both are touched within one minute.
- The research replay assumes a close at 15:25 when a position has not otherwise exited. The current live worker does not have that forced close. Results therefore are not a literal simulation of overnight behavior.
- The research selector permits at most one CE and one PE position at a time, and chooses an available strike when a signal appears. The live worker currently holds configured fixed symbols.
- Signal entries are assumed filled at the observed signal price. The live 5-second limit-fill window cannot be verified from one-minute candles. No buy-side slippage or spread is simulated.

Net point figures below deduct 0.25 point sell slippage; they do **not** yet include brokerage/taxes unless explicitly stated. One-point sell slippage is a stress test.

## Timeframe comparison

Nearest-ATM selection, one CE and one PE position at a time:

| Interval | Split | Trades | Win rate | Mean points/trade | Total points | Profit factor |
|---|---:|---:|---:|---:|---:|---:|
| 1m | Train | 573 | 75.92% | +0.102 | +58.65 | 1.038 |
| 1m | Validation | 198 | 75.25% | +0.020 | +3.95 | 1.008 |
| 1m | Test | 295 | 68.81% | −1.221 | −360.25 | 0.677 |
| 5m | Train | 238 | 68.91% | +0.362 | +86.05 | 1.082 |
| 5m | Validation | 72 | 62.50% | −0.895 | −64.45 | 0.822 |
| 5m | Test | 121 | 64.46% | −0.769 | −93.00 | 0.834 |
| 15m | Train | 114 | 54.39% | −0.087 | −9.95 | 0.987 |
| 15m | Validation | 41 | 58.54% | −1.804 | −73.95 | 0.706 |
| 15m | Test | 67 | 49.25% | −1.807 | −121.05 | 0.739 |

The 5m rule is relatively better in this test, but it is not profitable. With a rough current NSE option-fee proxy for 65 units plus a 1-point sell-slippage stress, the 5m nearest-ATM test replay is **−329.46 points** (121 trades). The rough fee estimate is ₹9,471 for those trades. The actual broker tariff and contract-note charges may differ.

## ML and neural-network comparison

The classifier learned whether a 5m SRSI candidate's subsequent rule-based trade would finish positive after 0.25-point sell slippage. The features were all available at signal time: option premium/momentum/range/volume, SRSI D, spot returns/volatility/trend, time of day, moneyness, and days to expiry. The model was trained on the first dates; a probability threshold was selected on validation dates only.

| Method | Validation selected trades / points | Test selected trades / points | Test points at 1-point sell slippage |
|---|---:|---:|---:|
| Logistic regression | 20 / −40.30 | 38 / −37.50 | −66.00 |
| Two-hidden-layer MLP | 21 / +19.60 | 19 / −40.20 | −54.45 |
| Nearest-ATM baseline | 72 / −64.45 | 121 / −93.00 | −183.75 |

Both models lost in the later period. The MLP looked better on validation and reversed on test, a classic instability warning. Validation log loss was 0.7129 for logistic and 0.6852 for the MLP; the constant validation-prevalence predictor is about 0.668, so neither model improved on that simple reference. The model trade counts are small and the simulated entries are not verified fills. **There is no model I recommend connecting to live orders.**

The MLP is a small NumPy-only two-hidden-layer network, so no large ML packages were installed on the 2-core VM.

## Strike and premium findings

The 5m data was also grouped by signal premium and by distance from spot. Positive step means ITM; negative means OTM. The selector rolls to that distance at each signal. These are research comparisons, not a static strike number.

- ₹50–₹100 premium: validation **−105.80 points** and test **+29.10 points** before charges at 0.25 sell slippage. Under the 1-point sell stress plus approximate fees, validation was **−191.02 points** and test **−84.00 points**. This band is not supported.
- 6 steps OTM (about 300 points): validation 35 trades / 4 active days; test 44 trades / 6 active days. Mean signal premium was ₹59.80 validation and ₹52.86 test. With 0.25 sell slippage and the estimated 65-unit charges, the results were roughly **+19.21 points validation / +20.45 points test**. With 1-point sell slippage plus those fees, they were **−7.04 / −12.55 points**.
- The same 6-OTM bucket was +30.05 points in train at the 1-point stress after modeled fees. Across all three periods together that is only about +10.46 points under the 1-point stress, before accounting for fill uncertainty and any spread/market-impact misspecification.
- Several other moneyness buckets turned positive on the test dates but lost on validation or training. Looking across many buckets and premium ranges makes the apparent best result vulnerable to selection bias.

The 6-OTM result is a *candidate to freeze and paper-test*, not a verified profitable strike rule. At the current 5-second limit-entry design, the minute data does not establish that its signals could actually be filled.

## Cost assumptions

Fee sensitivity used a 65-unit NIFTY lot as a historical proxy, ₹20 brokerage per executed order, NSE option transaction charges of 0.03553% on premium turnover, sell-side option STT of 0.15%, SEBI fee, 0.003% option stamp duty on buys, and 18% GST on brokerage/transaction/SEBI fees. It excludes spread, market impact, partial fills, and account-specific charges. References checked 30 Sep 2026:

- Zerodha published F&O/options charge schedule: https://zerodha.com/charges
- NSE fees, STT, and stamp duty page: https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies

## What I would test next

1. Freeze the 5m + 6-OTM hypothesis before collecting new dates; do not retune the offset or premium band on each new result.
2. Keep lot size fixed at one for research. Do not add loss-recovery sizing.
3. Use the already-running VM WebSocket recorder to collect timestamped 1-second prices. This is needed to reconstruct the provisional candle and check the five-second limit fills; Historify minute bars cannot answer that question.
4. Forward-paper-test on NIFTY first. The current SENSEX configuration needs its own BFO history and independent evaluation.
5. Add a rule to avoid immediate repeated zero-K re-entry only if the paper replay and live-worker state semantics are reconciled; this report deduplicated contiguous sampled zero-K runs.

## Files

- run_srsi_ml_research.py: data load, indicator reconstruction, interval replay, logistic and MLP comparison.
- summarize_strikes_premiums.py: premium and moneyness breakdown.
- cost_sensitivity.py: slippage/fee sensitivity.
- results/data_coverage.csv, results/baseline_summary.csv, results/model_comparison.json, results/strike_premium_breakdown.csv, results/cost_sensitivity.csv.
- results/events_1m.csv, results/events_5m.csv, results/events_15m.csv: candidate signals with features and simulated outcomes.

No live strategy configuration was edited or restarted. The research runner contains no OpenAlgo order API calls.
