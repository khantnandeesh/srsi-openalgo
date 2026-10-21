# SRSI options strategy (OpenAlgo)

StochRSI (4,4,2,2) option-buying strategy for NIFTY on OpenAlgo + Kotak, with a one-screen control page.

- `strategy.py` - per-contract worker: SRSI signals, limit buys, market exits, 15-point stop-loss, stop after SL
- `automatic_worker.py` - Automatic mode: picks a strike inside the premium band per side (Capture all is under development)
- `worker_supervisor.py` / `runtime_control.py` - thread start/stop over a Unix socket, recovery after restarts
- `max_loss.py` / `max_loss_mailer.py` - "Max loss today" across all threads, forced exit and email via OpenAlgo SMTP
- `config_ui.py` + `config.html` - control page (served on 127.0.0.1:8081, behind nginx + OpenAlgo login)
- `strategy_config.py` - settings validation

Runs on the VM from `/home/ubuntu/openalgo/strategies/config-ui`. Secrets (`.env`, alert emails, price-alert keys) and runtime state are not in git; see `.gitignore`. `workers.example.json` shows the config shape.
