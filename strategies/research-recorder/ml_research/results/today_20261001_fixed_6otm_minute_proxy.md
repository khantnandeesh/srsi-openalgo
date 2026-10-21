# 1 October 2026 NIFTY SRSI replay — minute proxy

Research only. No strategy configuration or live orders were changed.

## Fixed legs selected at the open

Synthetic put-call parity from the VM recorder estimated NIFTY near 22,577 at 09:15. Nearest expiry: 06OCT26.
- CE: `NIFTY06OCT2622900CE`
- PE: `NIFTY06OCT2622300PE`

## Result

5-minute provisional SRSI proxy; K=0 entry; no entry at or after 13:30; K>=80 profitable exit; 15-point stop; modelled fees plus one point adverse sell slippage.

| Metric | Value |
|---|---:|
| Trades | 8 |
| Gross premium points | 42.40 |
| Net premium points after modelled costs | 27.71 |

## Limitation

The full tick capture does not include these two fixed contracts for the full session, so this uses downloaded minute candles. It cannot validate intrabar SRSI, five-second limit fills, spread, or fill probability.

| Symbol | Entry | Entry price | Exit | Exit price | Reason | Net points |
|---|---|---:|---|---:|---|---:|
| NIFTY06OCT2622900CE | 2026-10-01 09:20:59 | 28.15 | 2026-10-01 09:30:59 | 31.10 | SRSI_EXIT | 1.15 |
| NIFTY06OCT2622900CE | 2026-10-01 10:15:59 | 30.20 | 2026-10-01 10:30:59 | 31.95 | SRSI_EXIT | -0.05 |
| NIFTY06OCT2622900CE | 2026-10-01 10:46:59 | 29.85 | 2026-10-01 13:26:59 | 14.85 | STOP | -16.77 |
| NIFTY06OCT2622300PE | 2026-10-01 09:50:59 | 38.15 | 2026-10-01 10:19:59 | 38.50 | SRSI_EXIT | -1.47 |
| NIFTY06OCT2622300PE | 2026-10-01 10:35:59 | 33.00 | 2026-10-01 10:45:59 | 36.30 | SRSI_EXIT | 1.49 |
| NIFTY06OCT2622300PE | 2026-10-01 11:41:59 | 35.80 | 2026-10-01 11:50:59 | 37.90 | SRSI_EXIT | 0.28 |
| NIFTY06OCT2622300PE | 2026-10-01 12:41:59 | 55.15 | 2026-10-01 12:45:59 | 79.60 | SRSI_EXIT | 22.55 |
| NIFTY06OCT2622300PE | 2026-10-01 13:08:59 | 88.50 | 2026-10-01 13:20:59 | 111.00 | SRSI_EXIT | 20.52 |
