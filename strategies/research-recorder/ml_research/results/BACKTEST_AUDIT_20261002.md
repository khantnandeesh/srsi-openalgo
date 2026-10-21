# Backtest correction: 2 October 2026

The earlier September results of INR 39,252 and INR 98,298 are not validated profitability evidence. Both reused make_events, which filtered the held contract price path by abs(strike-spot)<=300 before computing exits. It also computed K-to-zero transitions after that filter and dropped entries when the filtered future path could not produce an exit.

## Fixed 06OCT26 expiry, September, 5 lots

- Original helper: 73 trades, net +INR 35,462.11. Defective result; do not use as deployment evidence.
- Original entry filter with full exit monitoring: 80 trades, net -INR 39,844.05. The old helper discarded 22 signals based on future filtered exit availability.
- Intended exact ATM+300 CE / ATM-300 PE, full held-contract monitoring, exact timestamp spot mapping and genuine K transitions: 110 trades, gross -INR 35,571.25, net -INR 84,596.36.

Same 5m StochRSI 4,4,2,2, K-transition-to-zero entry, 13:30 entry cutoff, profitable K>=80 exit, strict 15-point stop, and 15:25 research square-off.

This is a 1-minute OHLCV proxy with assumed sample-close buys and 1-point sell slippage. It does not test actual 5-second limit fills. 54 corrected trades have at least one missing minute in the held contract path; the result is provisional, not a complete tick replay.

Script: ../fixed_expiry_dynamic_audit.py
Daily output: fixed_06oct26_dynamic_6otm_5lots_audit_daily.csv
Trade output: fixed_06oct26_dynamic_6otm_5lots_audit_trades.csv
Chart: fixed_06oct26_dynamic_6otm_5lots_audit.png

No live strategy settings, schedules, or orders were changed.

## Dynamic-expiry rerun
See CORRECTED_DYNAMIC_BACKTEST_20261002.md. Strict monthly P&L is incomplete; explicitly modeled full-month estimate is net -INR97,497.47.
