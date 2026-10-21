# Corrected dynamic backtest: 2 October 2026

Research only; no live strategy settings, schedules or orders changed.

## Rules
September2026, 5 lots=325 units, 5-minute SRSI4/4/2/2.
Current ATM+300 CE / ATM-300 PE, calculated from contemporaneous spot.
Seven downloaded active expiries are scanned independently; first K-to-zero transition wins.
Simultaneous same-side signals use lowest premium, then earliest expiry, then symbol.
One CE and one PE holding maximum; purchased strike and expiry stay locked through exit.
No entries at/after13:30. Profitable K>=80 exit or strict15-point stop; research EOD15:25.
The original backtest used K transitions into zero, not repeated buys at every zero sample.

## Fixed defects
Full held-contract price paths retained; true indicator transitions computed before eligibility.
No forward/backward-filled spot at entry. Minute close signals available at start+1minute.
No candidate removed because its future exit data are incomplete. CE/PE holdings isolated.

The prior +INR98,298.37 and +INR39,252 results reused the defective filtered simulator
and are invalid profitability evidence.

## Result interpretation
Strict result is INCOMPLETE: two bought contracts have no observed daily exit price.
Resolved-subset net -INR23,073.50 is NOT a monthly result.
API re-requests returned the same sparse26 and9 bars for these contracts.

Full-month sensitivity explicitly assumes a15:25 liquidation using the latest close already
available by15:25. Every such assumed price is flagged in the ledger, with source timestamps.
This variant has176 simulated trades: raw gross -INR7,426.25; modeled sell slippage INR57,200;
gross after slippage -INR64,626.25; fees INR32,871.22; estimated net -INR97,497.47.
Six positive and15 negative days across21 recorded sessions.
Three assumed EOD exits have price staleness6,4 and109 minutes.
122 trades have gaps in their held-contract minute history.

## Checks and limitations
All176 strike/expiry/timing/contract-lock checks passed.
20 independent K samples matched OpenAlgo SDK ta.stochrsi.
Ledger totals and exit-price availability invariants passed.
One-minute OHLCV proxy; no actual tick/1-second replay or five-second limit-fill verification.
Stops hidden in data gaps and actual market fills remain unknown.
The full-month result is an assumption-dependent estimate, not verified executable P&L.

Correction to earlier fixed-expiry response: -INR35,571.25 was gross BEFORE both slippage
and fees; after modeled slippage it was -INR71,321.25 before fees.

## Reproduce on VM
venv/bin/python strategies/research-recorder/ml_research/corrected_dynamic_expiry_backtest.py
venv/bin/python strategies/research-recorder/ml_research/corrected_dynamic_expiry_backtest.py --model-missing-eod

Strict outputs use corrected_dynamic_6otm_5lots_september_2026_*.
Full-month sensitivity outputs add _modeled_eod.
Daily CSVs, trade ledgers, method JSON, coverage and PNG graph are in this results directory.
