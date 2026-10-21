# SRSI runtime thread controls

Deployed to `https://openalgo.nandeesh.dev/strategy-config/` on 8 October 2026.

## Use

1. Start the existing SRSI strategy from Python Strategies, or let its schedule start it.
2. Each saved thread has an On/Off toggle. Starting an Off thread creates a new worker using its saved configuration and shared rules.
3. Stopping a thread blocks further buys. If an order or position is still active, its original order management and exit rules continue. The row says **Stopping after exit**, stays visible, and its settings are blurred and disabled.
4. Once the position is confirmed closed, the worker disconnects and finishes. The toggle becomes **Off**, settings unlock, and the saved row remains available for editing or starting again.

An Off thread must have its settings saved before starting. While the SRSI process is stopped, toggles retain their **at next start** behavior. The config page never launches the overall strategy process.

Active rows cannot be edited or removed through either the UI or config API. A pending/partial sell does not count as a completed exit. Unknown order outcomes remain locked for reconciliation. Stop requests persist across process restarts, and a drain restart checks for working broker orders before continuing.

## Trading behavior

The existing candle calculation, SRSI calculation, entry conditions, premium filters, quantities, sell-at-or-above-entry condition, stop loss, and fill-management methods are retained. The added lifecycle wrapper blocks buys during a stop request and retires a worker only after confirmed flat state. It does not force a sell or change an indicator threshold.

## Components

- `strategy.py`: worker lifecycle flags and wrapper; starts the supervisor.
- `worker_supervisor.py`: owns actual thread instances, handles independent start/stop requests, prevents duplicates, and waits for thread termination before unlocking a stopped row.
- `runtime_control.py`: private Unix-socket RPC, process lock, atomic configuration writes, saved stop intents, and stale-save checks.
- `config_ui.py`: runtime status/command endpoints alongside the existing form API.
- `config.html`: live status polling, toggles, locked/blurred draining rows, and normal editing after exit.

Private IPC: `runtime.sock`; process lock: `runtime.lock`; saved stop requests: `runtime_stop_intents.json`. Status uses live IPC and process-lock checks, not PID files alone.

## Verification

- 30 isolated VM checks passed using fake broker methods; no broker orders submitted. Original trading/indicator/fill method ASTs compared with the pre-change source.
- 17 browser checks passed with all API calls intercepted: start, stop-after-exit, blur, edit/remove locks, reload persistence, off/unlock after exit, restart, independent rows, unavailable-status locks, no JS errors, and mobile width.
- The actual public page was inspected without clicking trading controls: HTTP 200 runtime status, four saved rows/four toggles, no JavaScript errors, supervisor offline.
- Deployed files matched verified staging files; saved `workers.json` was unchanged. Only the configuration-page process was restarted.

VM backup: `/home/ubuntu/openalgo-change-backups/runtime-controls-20261008/before-live/`.
VM isolated verification: `/home/ubuntu/openalgo-change-backups/runtime-controls-20261008/staged/verification-results.json`.

The production SRSI process was not started during deployment or verification.
