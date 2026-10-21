# OpenAlgo / Only SRSI — handoff

Updated: **10 October 2026, IST**. Intended for a developer or AI agent taking over this deployment. Paths and current settings were inspected on the VM. This document contains no credentials.

## 1. Start here

We maintain a customized OpenAlgo deployment on an Ubuntu VM. OpenAlgo handles broker authentication, market data, orders, positions, Analyze mode, Python strategy scheduling and the main dashboard. Our custom **Only SRSI** engine and its configuration page live alongside that application.

- Website: https://openalgo.nandeesh.dev/
- Custom page: https://openalgo.nandeesh.dev/strategy-config/
- Python Strategies: https://openalgo.nandeesh.dev/python
- VM repository: `/home/ubuntu/openalgo`
- VM Python: `/home/ubuntu/openalgo/venv/bin/python`
- Custom module: `/home/ubuntu/openalgo/strategies/config-ui`
- SSH alias from this laptop: **`myvm`**.

The user wants work and research processing done on the VM. Use the laptop for access, documentation and necessary editing/staging. Do not start strategies, enable threads or change trading settings just to inspect or repair the UI.

### Current snapshot — not a permanent configuration

At inspection on 10 October:

- Main `openalgo.service` is active; the configuration-page process is also present.
- Only SRSI's runtime controller is offline; no running worker records were returned.
- Saved mode is `manual`; nine manual rows are saved, all disabled.
- Automatic mode is disabled; saved index NIFTY, expiry `13OCT26`, one lot per worker, five-minute interval, premium ₹100–₹200, search depth 15.
- Stop-intent file is empty.
- Scheduler metadata says Only SRSI is scheduled **09:20–16:00, Monday–Friday, NSE**. Last recorded stop was 9 October at 16:00 IST.

Read current state before acting. A stopped controller or disabled row does **not** prove the broker account has no positions or working orders.

## 2. SSH access and connection reuse

Laptop: `/Users/khantnandeesh`. SSH configuration: `/Users/khantnandeesh/.ssh/config`. Existing private key path: `~/.ssh/opencode_aws_key.pem`.

The existing alias resolves to:

```sshconfig
Host myvm
    HostName 35.154.104.158
    User ubuntu
    IdentityFile ~/.ssh/opencode_aws_key.pem
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 1h
```

Use the existing alias, rather than repeating a hostname and key on every command:

```sh
ssh myvm
ssh myvm 'cd /home/ubuntu/openalgo && git status --short'
ssh -O check myvm
scp /path/to/file myvm:/home/ubuntu/openalgo/some/path/
```

The first connection creates the shared transport; subsequent commands reuse it. The connection can expire after an hour. `ssh -O check myvm` reporting no socket then is normal; the next SSH call creates one.

During this inspection, one initial connection failed with `banner exchange ... invalid format`; a connection bypassing multiplexing succeeded and ordinary `ssh myvm` then succeeded. This was not a private-key format failure. If it recurs, diagnose the transport before changing keys:

```sh
ssh -O check myvm
ssh -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=10 myvm 'hostname'
```

Use that bypass only for diagnosis, then return to `myvm` reuse. Do not print, copy into chat or commit the private key or any API credentials. The Codex environment may require network escalation for SSH; request it through its command tool when needed.

## 3. The OpenAlgo folder

All paths below are relative to `/home/ubuntu/openalgo`, unless shown as absolute.

| Location | Purpose |
| --- | --- |
| `app.py` | Main OpenAlgo application entry point. |
| `blueprints/` | Dashboard and HTTP routes, including Python Strategies and P&L. |
| `blueprints/python_strategy.py` | Python strategy registration, execution and scheduler integration. |
| `broker/kotak/` | Kotak broker integration; REST and streaming adapters. |
| `services/` | Application services, including WhatsApp and indicator services. |
| `restx_api/` | OpenAlgo API routes consumed by SDK clients. |
| `frontend/src/` | Main dashboard frontend source. |
| `frontend/dist/` | Built dashboard assets; separate from our standalone config HTML. |
| `websocket_proxy/` | OpenAlgo streaming infrastructure. |
| `sandbox/` | Analyze-mode order, position and fund logic. |
| `database/`, `db/` | Database code and persisted application databases. |
| `strategies/strategy_configs.json` | Python Strategies registrations, scheduling metadata and recorded process state. |
| `strategies/scripts/` | Registered scripts and older research/backtest scripts. |
| `strategies/config-ui/` | Our current Only SRSI source, UI, settings and recovery files. |
| `strategies/research-recorder/` | Separate historical-data/research work; not the active Only SRSI engine. |
| `strategies/box-arbitrage/` | Separate box-arbitrage work; not part of Only SRSI. |
| `log/strategies/` | OpenAlgo's captured strategy process output. |
| `venv/` | Installed Python runtime and dependencies used by this VM. |
| `.env` | Protected application environment; do not include its contents in handoffs. |

The repository has substantial uncommitted custom changes and untracked research/module files. **Do not reset, clean or overwrite the deployment wholesale.** Inspect differences and back up the specific files being changed. Git alone does not capture every deployed custom file.

## 4. How Only SRSI is launched

The UI registration is named **`only srsi`**. Its registered entry point is:

`/home/ubuntu/openalgo/strategies/scripts/strategy_20260925091130.py`

This file is a small wrapper. It sets `OPENALGO_LIVE_TRADING=True`, adds `strategies/config-ui` to Python's import path, and uses `runpy.run_path(..., run_name="__main__")` to execute `config-ui/strategy.py`.

```text
OpenAlgo Python Strategies / scheduler
  → strategy_20260925091130.py
  → config-ui/strategy.py: main()
  → WorkerSupervisor
      → Manual: OptionLegWorker for each enabled saved row
      → Automatic: AutomaticWorker → selected OptionLegWorker
```

The scheduled wrapper uses normal OpenAlgo order APIs. **Analyze mode in OpenAlgo determines whether these API orders are simulated.** Do not assume that `OPENALGO_LIVE_TRADING=True` alone identifies the broker mode. Directly running `config-ui/strategy.py` defaults to its separate internal paper behavior when that environment flag is false; that is different from routing normal API orders through Analyze mode.

The config page does not launch the overall SRSI process. Start that parent from Python Strategies, or let its schedule start it. The supervisor remains alive even when all workers are off, so later UI start requests can create workers.

## 5. Trading rules in plain language

A **leg/worker/thread** is one independently managed option contract. CE means call; PE means put. `SELLING` means we already bought and are managing the long position until its exit—it does not mean opening a short option position. **Flat** means no held units and no unresolved order.

Current shared settings:

- Product `NRML`.
- StochRSI RSI length **4**, lookback **4**, K smoothing **2**, D smoothing **2**.
- Buy when **K equals 0**, subject to the entry premium band and lifecycle checks.
- Indicator exit when **K ≥ 80 AND LTP ≥ average entry price**.
- Stop exit when **LTP < average entry price − 15 points**. The comparison is strictly below.
- Manual premium band currently ₹0–₹10,000,000; automatic entries also enforce their ₹100–₹200 band.
- Lots are fixed user settings; native contract lot size is resolved at runtime. Quantity = configured lots × resolved lot size. No live martingale sizing is part of this engine.

Example: entry ₹150 → stop condition below ₹135. At K=90 and LTP=₹145, the engine holds because price is below entry. At K=90 and LTP=₹151, it submits an exit. A gross break-even exit can still lose after costs.

### Prices, candles and SRSI

1. Resolve the contract and its lot size using the selected index, expiry, strike and side.
2. Load historical completed candles from OpenAlgo.
3. Receive the option's live LTP through the SDK WebSocket.
4. Combine completed candle closes with the current LTP as the **provisional close of the running candle**.
5. Recompute K/D for that provisional close; commit a completed close when the interval advances.

This is intrabar calculation: it does not wait for a five-minute candle to close before evaluating signals. StochRSI uses closes; it does not require the running candle's entire O/H/L/V to compute this indicator. Five-minute and fifteen-minute intervals can produce different values at the same time.

Candles are anchored at **09:15 IST**. The last observed LTP estimates the completed close at a boundary. Missing ticks/gaps can therefore make this history differ from exchange historical bars; the engine does not reconstruct unseen ticks. Signal processing rejects stale queued ticks, with a three-second freshness threshold. Order polling remains roughly once per second; signal evaluation is driven by queued WebSocket ticks.

### Orders and market hours

- Entry is a **LIMIT BUY at observed LTP**, with a five-second fill window.
- A still-working buy is cancelled and reconciled; actual filled quantity and average govern the held position. Cancellation requests alone do not prove the order is finished.
- Exits are **MARKET SELLs** for the confirmed remaining quantity. Pending/partial outcomes remain tracked.
- Ambiguous submissions halt that worker to avoid duplicate orders.
- New entries: **09:15 ≤ time < 15:15 IST**. Price-based exit checks operate before **15:30 IST**.
- The scheduler's 16:00 stop is a process stop, **not a guaranteed position square-off**. The engine has no unconditional end-of-day liquidation rule. A stopped process cannot continue its client-managed stop checks.

## 6. Manual and automatic modes

### Manual

The user selects each worker's name, index, available expiry, CE/PE, valid strike, candle interval and lots. Each enabled row becomes its own worker. Intervals can differ between rows. All rows share the SRSI/product/stop settings listed above.

### Automatic

There are at most **two automatic workers: `AUTO-CE` and `AUTO-PE`**. The user still controls index, expiry, lots per worker, interval and shared rules. Expiry is **not selected dynamically across multiple expiries**. The strategy chooses strikes within that one configured expiry:

1. Use fresh underlying spot and listed strikes to locate nearest ATM; midpoint ties round upward.
2. Check ATM first.
3. CE: check up to 15 higher listed strikes, then up to 15 lower ones.
4. PE: check up to 15 lower listed strikes, then up to 15 higher ones.
5. Select the first candidate with a fresh premium inside ₹100–₹200, inclusive.
6. Load that contract's own history and run the same SRSI entry/exit engine.

This is **not** the older research proposal of fixed six-strikes-OTM selection. It does not rank all strikes for expected profitability or choose whichever signals first across all expiries.

While waiting to buy, reselect if the selected premium leaves the band. While holding or resolving a pending order, keep the exact contract locked until its trade is resolved. CE and PE manage their own selections and positions independently. No eligible fresh quote means wait/retry, not manufacture an order.

Automatic mode now shares a reference-counted WebSocket connection across spot, candidates and selected children. Metadata is cached for 60 seconds and refreshed when the selection window needs updating. Fresh quote handoff preserves the original receive time. Selection can proceed immediately when required quotes are ready; missing higher-priority quotes retain a two-second grace window.

## 7. Frontend → configuration → running workers

```text
Browser config.html
  → config_ui.py (loopback HTTP port 8081)
      → strategy_config.py validates settings
      → workers.json persists configuration atomically
      → runtime_control.py uses runtime.sock for live actions
          → WorkerSupervisor starts/stops/locks individual workers
```

The public `/strategy-config/` surface reaches this loopback service through deployment routing; its proxy mapping was not independently located during this handoff. Main OpenAlgo runs at loopback port 5000. The config page's Trading Helper uses server-side market requests plus a separate shared WebSocket relay exposed to the browser as SSE; it is distinct from the trading workers' feed.

The page has manual/automatic switching, expiry/index dropdowns, per-worker candles/lots, toggles, automatic selection/status cards, a Trading Helper chain, custom centered confirmations and a minimal black/white design. Clicking a chain `+` adds a configuration row; it is not an order placement action by itself.

Configuration saves are revision-checked and atomic. Active rows cannot be edited or removed through the UI/API. Start/stop controls are explicit runtime actions, not implicit process restarts. When the parent is offline, saved enable flags govern the next run. When it is online, starting a saved worker creates it without restarting the parent.

**Stop after exit:** block new buys; if flat, retire; if held/pending, retain the original management rules until confirmed flat. Settings stay locked while draining. This action does not force an immediate sell.

Mode changes require workers ready, waiting to buy, flat, without pending orders or halt/recovery conditions. Mode switching stops eligible flat workers, changes mode and leaves automatic disabled until explicitly started.

Closing the browser does not stop strategy threads. They run in the VM's SRSI process. Conversely, a visible page does not prove that process is running.

### Local HTTP endpoints

| Method / path on port 8081 | Purpose |
| --- | --- |
| `GET /api/config` | Saved normalized configuration and revision. |
| `POST /api/config` | Validate and save settings; may reject conflicts/active-row edits. |
| `GET /api/runtime` | Controller state and worker/automatic diagnostics. |
| `POST /api/runtime` | `start`, `stop_after_exit`, `set_mode`, `automatic_start`, `automatic_stop`. These mutate trading lifecycle. |
| `GET /api/expiries` | Available expiries for selected underlying. |
| `GET /api/strikes` | Strike selector data. |
| `GET /api/chain` | Trading Helper chain data. |
| `GET /api/live` | Helper SSE prices. |

The standalone config service has no application login check; the user previously requested removing config-page authentication. It binds to loopback, but public accessibility depends on routing. Never assume the main dashboard session automatically protects these endpoints. Credentials stay on the server, not in the HTML/SSE.

## 8. Important custom files

Everything in this table is inside `/home/ubuntu/openalgo/strategies/config-ui`.

| File | Responsibility |
| --- | --- |
| `strategy.py` | Contract resolution, candle/history handling, SRSI, orders/fills, stop/exit logic and external-position synchronization; creates the supervisor. |
| `worker_supervisor.py` | Owns worker instances, prevents duplicate starts, handles lifecycle commands and protects mode transitions. |
| `automatic_worker.py` | Premium-based strike selection, selected-child lifecycle, contract lock and restart journal. |
| `strategy_config.py` | Supported indexes/exchanges, settings validation, normalization and automatic worker rows. |
| `runtime_control.py` | Unix-socket requests, process/config locks, atomic JSON persistence and revision hashes. |
| `config_ui.py` | Standalone HTTP server, config/runtime endpoints and helper market requests. |
| `config.html` | Standalone frontend HTML/CSS/JS; changing this does not require rebuilding the main React dashboard. |
| `config_market_feed.py` | Trading Helper WebSocket hub / SSE snapshots. |
| `shared_market_feed.py` | Automatic trading shared feed, subscription ownership and fresh cached handoff. |
| `strategy_caches.py` | Bounded history cache; contract/exchange/interval/host key, independent copies, candle/60-second invalidation. |
| `incremental_srsi.py` | Incremental Wilder RSI and SRSI previews; `strategy.py` uses SDK fallback at decision boundaries/near-flat RSI windows. |
| `price_alerts.py` | Price alert logic; sending currently disabled by prior request. |
| `workers.json` | Saved shared rules, manual rows, selected mode and automatic settings. |
| `.env` | Protected custom-module API credentials/environment. |
| `price-alerts.private.json` | Protected sender/recipient/Resend settings and sending enable flag; never share contents. |
| `price-alerts.sqlite3` | Alert state/cooldown persistence. |
| `logs/` | Per-contract engine logs. |
| `config-ui.log`, `config-ui.pid` | Config-server output and recorded PID; validate actual process, not only the PID file. |
| `runtime.sock`, `runtime.lock` | Live controller IPC and exclusive parent-process lock. |
| `workers.lock` | Configuration write lock. |
| `runtime_stop_intents.json` | Persisted stop-after-exit requests. |
| `automatic_contracts.json` | Selected-contract recovery journal, created/updated as needed. |
| `automatic_status.json` | Historical automatic diagnostics; historical records are not proof of current activity. |
| `PERFORMANCE.md`, `RUNTIME_CONTROLS.md`, `AUTOMATIC_MODE.md`, `CONFIG_UI.md` | Supporting notes; some older descriptions predate current runtime controls/feed optimizations. Prefer current code and this handoff. |

Do not delete recovery/lock/journal files as a shortcut to clear a warning. Verify current positions and terminal order states, then use the intended reconciliation path. Files may be absent when unused.

## 9. Recovery and known practical limits

- Startup checks existing long quantity and average price; unexpected short positions or missing averages block trading.
- Automatic selection journals frozen contract settings before running the selected engine. Recovery checks held positions and working orders before reselection.
- Stop intents survive parent restarts. A recovered draining worker should finish its exit without enabling new buys.
- While the parent runs, an external UI exit is checked through shared account position reads approximately every five seconds. Matching outstanding orders and direct quantity confirmation are checked before changing state.
- Confirmed external full exit moves an enabled worker back to waiting-to-buy; a draining worker can finish. A new entry needs a fresh subsequent tick. Partial external exits retain only the remaining quantity.
- Failed position/order checks pause signal actions; unexpected larger/short quantities halt for attention.
- **Offline external exits are not automatically cleaned up by a stopped controller.** Recovery pending can persist until SRSI runs and reconciles, or a carefully verified administrative reconciliation is performed.
- Manual startup does not provide universal reconciliation of every possible pre-existing working order. Recovery protections must not be described as a guarantee against every failure case.
- Account positions aggregate identical symbol/exchange/product exposure. Separate worker state does not isolate broker exposure from another strategy or manual trading in the same contract.
- Stops depend on a running process and fresh feed; no broker-held protective stop was added. Gaps/slippage can exceed the configured point loss.
- Hardcoded intraday times do not replace exchange holiday/special-session handling.
- API rate limits, stale quotes, broker authentication and unavailable market data can prevent entries/exits. Never treat unavailable data as a confirmed zero position.
- Correct indicator calculations and fast execution do not establish a profitable strategy.

## 10. Inspection and operations

These are read-only checks:

```sh
ssh myvm 'systemctl is-active openalgo'
ssh myvm 'systemctl show openalgo -p ExecStart -p WorkingDirectory'
ssh myvm 'curl --max-time 15 -sS http://127.0.0.1:8081/api/runtime'
ssh myvm 'tail -n 80 /home/ubuntu/openalgo/strategies/config-ui/config-ui.log'
ssh myvm 'ls -lt /home/ubuntu/openalgo/log/strategies/strategy_20260925091130_* | head'
ssh myvm 'ls -lt /home/ubuntu/openalgo/strategies/config-ui/logs/ | head'
ssh myvm 'journalctl -u openalgo -n 80 --no-pager'
```

Strategy process logs look like `log/strategies/strategy_20260925091130_20261009_100645_IST.log`. Engine logs include contract resolution, history, WS subscription, status, buy/exit signals, fill reconciliation and external-exit confirmation. Inspect narrowly; redact account details/credentials before sharing raw logs.

Main service details:

- Unit: `/etc/systemd/system/openalgo.service`.
- Working directory: `/home/ubuntu/openalgo`.
- Command: `/home/ubuntu/openalgo/venv/bin/python app.py`.

For an explicitly authorized main-application restart:

```sh
ssh myvm 'sudo systemctl restart openalgo'
```

Check strategy/position state before a restart. It can affect scheduling and broker/feed services. The separate configuration server is currently a `python -u config_ui.py --port 8081` process with a PID file; it is **not restarted by `systemctl restart openalgo`**. Confirm its launch arrangement before replacing it. Do not start a second server blindly on the same port.

`config.html` is read from disk on requests, so HTML-only changes appear after refreshing. Python module changes need the corresponding process restarted; trading-engine changes load on the next SRSI start. Updating the page never authorizes starting trading.

The VM did not have `rg` installed during inspection. Use `grep`/Python there if needed. SDK introspection is safer from `/tmp`: running Python from the repository root can shadow the installed `openalgo` SDK with the source package.

## 11. Recent fixes and evidence

9 October changes retained in the deployed engine:

1. External UI position exits reconcile while the worker is running; flat enabled workers resume entry monitoring, paused workers retire.
2. Automatic CE/PE/scanning/selected contracts share one persistent socket with reference-counted subscriptions.
3. History cache refreshes overlapping recent dates instead of repeatedly downloading full history.
4. Incremental SRSI previews replace repeated full-history calculation, with SDK reference checks at sensitive boundaries.
5. No unconditional two-second selection wait when the needed fresh quotes are already ready.

VM offline verification for the performance change: **24 checks passed**, **6,480 previews matched the installed SDK with observed K/D difference zero**. A CPU benchmark measured approximately **3× faster** calculation. Fake broker/feed clients were used; no orders/messages were sent. This does not measure real market/network latency. Live-session performance still needs observation. These changes were installed while SRSI remained stopped and load on its next start.

WhatsApp `/pnl` was also patched in `services/whatsapp_bot_service.py`: SDK requests use loopback port 5000 instead of a stale public host, timeout 20 seconds, and produce a formatted position/P&L summary. It uses a paired WhatsApp Web session; self-sent commands are accepted. No test message was sent during that fix, so successful recipient delivery was not established. Email alerts remain disabled.

## 12. Backups, local work and next-agent checklist

VM backups are outside the repository at `/home/ubuntu/openalgo-change-backups/`. Useful verified locations:

- Performance: `/home/ubuntu/openalgo-change-backups/automatic-performance-1791565050`.
- External exits: `/home/ubuntu/openalgo-change-backups/external-exit-sync-1791557938`.
- WhatsApp P&L: `/home/ubuntu/openalgo-change-backups/whatsapp-pnl-1791563653`.
- Config-save fixes: `/home/ubuntu/openalgo-change-backups/config-save-fix-1791485269`.
- Automatic status: `/home/ubuntu/openalgo-change-backups/auto-status-fix-1791557084`.
- Runtime controls: `/home/ubuntu/openalgo-change-backups/runtime-controls-20261008/before-live/`.

Local implementation notes/copies: `/Users/khantnandeesh/openalgo-notes/`, especially `automatic-performance/`, `automatic-mode/`, `runtime-controls/`, `external-exit-fix/`, `auto-status-fix/`, `config-error-fix/` and `whatsapp-pnl-fix/`. These are historical working copies, not necessarily the latest deployed source. Performance staging/report: `/home/ubuntu/openalgo/strategies/config-ui/.automatic-performance-20261009/`.

Before changing anything:

1. Read this handoff, connect using `myvm`, inspect current deployed code and Git differences.
2. Check runtime status, scheduler metadata, positions and orders relevant to the requested change.
3. Identify whether the change affects the standalone page, its server, SRSI engine or main OpenAlgo application.
4. Back up the exact affected files; preserve workers, recovery journals and private settings.
5. Implement the authorized change without starting strategies or sending messages implicitly.
6. Run tests only when requested/authorized. Use fake clients for isolated checks; an order API call may trade if Analyze mode is off.
7. Restart only the necessary process when authorized; report what was verified and what still requires live observation.

Suggested first request to a new agent: **“Read `/home/ubuntu/openalgo/OPENALGO_HANDOFF.md`, use the existing SSH alias `myvm`, inspect live state before changes, and keep trading stopped unless I explicitly ask to start it.”**
