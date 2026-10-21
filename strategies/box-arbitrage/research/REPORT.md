# Box arbitrage research — 7 October 2026

## Conclusion
The completed OHLC approximation trades were losing. This is not an executable historical arbitrage backtest. Historify and archived files have no historical bid/ask depth.

## Study setup
All processing ran on the VM. Historify minute bars plus previously downloaded September CSVs; 21 spot-data dates between September 1 and 30, 2026. Pinned expiry 29SEP26, 1 assumed lot = 65 units; strike widths 100/200; ATM +/- 5 strikes; one box attempt per day; completed-minute signals and next-minute open simulated entry/exit.
Minimum estimated entry edge INR200. Exit estimated net profit INR100, loss INR1000, 15:20 forced-exit signal. Actual execution latency, sequential fills and historical margin are not reproduced.
Entry valuation subtracts 10% annual capital opportunity cost through expiry. Realized proxy subtracts elapsed holding-time opportunity cost on INR50000, not the entire expiry-period funding estimate.

## Published fees
Kotak API brokerage INR0/order; alternative standard F&O brokerage INR10/order. NSE option transaction + IPFT 0.03553% each side; SEBI 0.0001%; STT 0.15% sell premium; stamp duty 0.003% buy premium; GST18% on brokerage + transaction/IPFT + SEBI. Charges estimated without contract-note daily rounding.
Sources: https://www.kotakneo.com/pricing/trade-free-plan/ ; https://www.kotakneo.com/calculator/brokerage-calculator/ ; https://nsearchives.nseindia.com/content/circulars/FA73061.pdf ; https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax

## Pinned September expiry outcomes
| Model | Closed | Unresolved | Net on closed boxes (INR) |
|---|---:|---:|---:|
| kotak_api | 2 | 4 | -678.41 |
| standard_10 | 2 | 2 | -1001.53 |
| slippage_050 | 2 | 2 | -1228.42 |
| slippage_100 | 0 | 2 | 0.00 |
| original_budget | 0 | 0 | 0.00 |

Unresolved entries/exits are not included in P&L. Four dates lack eligible synchronized quartets. These results do not establish total account P&L. Different cost/slippage assumptions alter entry eligibility, so variants may contain different trades.

## Auto expiry coverage
Selecting nearest expiry found in archive is only a diagnostic: actual nearest listed expiries are not fully archived. With combined archive, 18 dates have no eligible quartet and three have no qualifying signal; no trades. This cannot validate production auto-expiry logic.

## Corrections to preliminary run
The initial Historify-only estimate (-INR1063.89) is superseded: it omitted archived September data, treated no-data days as no-signal days, and charged full expiry-period funding at intraday exit. Final funding treatment matches actual holding time.

## Limitations
- No historical bid/ask, order book, margin or fill sequence. Simultaneous four-leg fill assumption.
- Minute prices are separate last trades; synchronized timestamps do not guarantee executable quotes.
- Nearest expiry in available archive may differ from actual nearest listed expiry; archive coverage incomplete.
- 65-unit lot assumption; empty historical metadata. Current master is not historical proof.
- Unresolved entries/exits excluded from resolved P&L and explicitly counted.
- Capital proxy is long premiums times 1.25, not staged basket-margin verification.
- 10% capital opportunity cost through expiry in entry gate; elapsed holding-time opportunity cost deducted at exit using conservative 50000 capital proxy.
- No parameter optimization; no orders sent.

## Files
backtest.py; pinned_29SEP26/summary.json; pinned_29SEP26/*_daily.csv; pinned_29SEP26/*_trades.csv; pinned_29SEP26/daily_pnl.png

No production strategy was started, mode changed or order submitted.
