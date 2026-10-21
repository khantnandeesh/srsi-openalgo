# Strategy configuration page

The page edits `workers.json` only. Strike choices come from the selected
index/expiry option chain. Each worker stores a lot count; at startup,
`strategy.py` resolves the exact contract and lot size, then calculates order
units as `lots × lot size`. The OpenAlgo scheduled entry point
`strategy_20260925091130.py` delegates to `strategy.py`, which receives live LTP
through the OpenAlgo SDK WebSocket.

Saving does not start or restart a worker and does not change the OpenAlgo
schedule. New settings take effect the next time the scheduler starts the
strategy. The scheduled entry point enables live orders to preserve the
existing scheduled strategy behavior; running `strategy.py` manually defaults
to paper mode. The configuration service needs `OPENALGO_API_KEY` in its
server-side `.env` file to load option-chain strikes; the key is never sent to
the browser.

Run the page manually when you want to edit settings:

```sh
python config_ui.py
```

Then open `http://127.0.0.1:8081`. The server binds only to loopback. If the
project is on the VM, access it through an SSH tunnel from your laptop:

```sh
ssh -L 8081:127.0.0.1:8081 openalgo-aws
```

Keep that SSH session open and browse to `http://127.0.0.1:8081` on the laptop.
Saving changes updates the configuration file consumed by the scheduled
strategy at its next start. The live price feed in `strategy.py` uses OpenAlgo's
SDK WebSocket LTP subscription.

## Per-worker startup settings
Each worker has its own candle_minutes (1,3,5,10,15,30) and enabled boolean.
Legacy files inherit their previous shared candle interval and enabled=true.
Disabled rows remain saved but create no worker/client/orders at startup.
All-off configurations exit without creating workers. Saving never hot-reloads
an active worker or places an order. Each running contract retains its state.
The chain helper uses server-side read-only market requests, then a shared
WebSocket LTP relay. Browser event streams contain prices only, no API key.
+CE/+PE adds an unsaved contract row; prices are snapshots until quote updates
arrive. The helper subscribes only the displayed 21 strikes and underlying.
