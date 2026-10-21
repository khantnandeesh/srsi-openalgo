# Automatic-mode performance changes — 9 October 2026

- One reference-counted LTP socket serves automatic CE, PE, spot and selected SRSI children. REST/order clients and position state remain per worker.
- Scan/reselection and 60-second metadata refresh preserve the socket. Subscription updates add/remove only the needed contracts; the last consumer closes the connection.
- A new consumer can replay a cached quote only while it is fresh. Replay preserves the original receive time and exchange/SDK timestamp.
- Candidate selection proceeds once all higher-priority candidates have fresh quotes. Missing higher-priority quotes retain the prior two-second grace before the scan can skip them.
- History is cached by contract/exchange/interval/host, with independent worker copies. Cached history expires after 60 seconds or when the candle changes. Refreshes merge overlapping recent dates, trim old dates and retain candle-close filtering.
- Incremental RSI/StochRSI commits completed closes only. Each live close is a preview. Exact decision boundaries and nearly flat RSI windows use the SDK reference calculation; repeated unchanged prices reuse the result.
- Diagnostics include shared-feed consumer/subscription counts, connection counts, selection time, history-cache outcome and indicator-cache/reference counts.

## Verification on the VM

24 offline checks passed, using fake broker/feed clients. No orders or messages were sent.
6,480 SRSI previews across six patterns and four parameter sets matched the installed SDK exactly; maximum observed K/D difference was zero.
The calculation benchmark used 1,000 completed closes and 1,500 changing LTPs:
- Before: 276.27 ms total.
- After: 91.91 ms total.
- Approximately 3.0x faster for this CPU workload.

Checks include subscription ownership, selected-child handoff, metadata refresh without reconnection, stale-cache rejection, history isolation and candle invalidation, and held/pending contract locks.

The timing is an offline CPU benchmark. Live network/broker latency and market-session operation still need observation. The strategy was not started during deployment. Changes load on the next SRSI process start.
