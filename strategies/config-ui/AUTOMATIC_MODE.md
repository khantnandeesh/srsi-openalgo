# Automatic premium selection

The scheduled SRSI entry point keeps the runtime supervisor available. The configuration page switches between Manual and Automatic. Mode changes stop only flat workers and refuse a change while a worker is holding, has a pending order, is halted, or is still reconciling startup.

Automatic mode starts at most AUTO-CE and AUTO-PE. Index, expiry, lots per worker and candle interval remain user settings. The common SRSI settings remain on the page. Manual contracts remain saved separately.

For each selection, use the fresh spot feed to find nearest listed ATM (midpoint rounds up). Check ATM, then up to 15 listed strikes outward (higher CE, lower PE), then up to 15 inward. Pick the first fresh eligible LTP within the inclusive 100–200 range. Subscription changes have a two-second settling window. Stale or unavailable quotes are skipped. When there is no match, wait and retry; do not place an order. Broker expiry and symbols come from the option chain, not from constructing an assumed strike list.

The selected contract gets its own completed history and existing intrabar SRSI engine. Entries and exits keep the current SRSI rules, with the automatic premium band enforced for new entries. No old contract indicator values carry over to a new selection. The engine resolves native lot size, multiplying by configured lots.

A flat selected contract is retired before evaluating a signal when its live premium leaves 100–200. Its WebSocket disconnects before scanning/selecting the next contract. Positions and pending orders lock the contract; the original sell and stop-loss rules keep running. CE and PE reselections are independent.

`automatic_contracts.json` records the selected contract and frozen settings before the engine runs. On restart, recover that contract and check the existing position and working orders before scanning or placing a new buy. An unknown working order blocks recovery. Clear the journal only after confirmed flat retirement. Stop intents persist; a recovered stop does not enable trading again after the exit.

Live UI fields show selection spot/ATM, contract symbol, LTP, SRSI K, actual units and holding/pending status. These are observational values; use the trade/order book to reconcile broker state.

Email sending is disabled in `price-alerts.private.json`. No email API requests are part of automatic mode.

Validation: isolated fake-feed/worker checks cover selector ordering, fallback, boundaries, stale quotes, order/position locking, recovery, two-worker limit, stop intents and mode transitions. Mocked browser checks cover autosave, controls and mobile layout. No live order tests.
