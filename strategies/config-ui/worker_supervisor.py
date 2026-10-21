"""Own worker lifetimes; does not calculate signals or submit orders."""
from __future__ import annotations

from contextlib import ExitStack
import copy
import fcntl
import json
import os
import signal
import socketserver
import threading
import time

import max_loss

from runtime_control import BASE, CONFIG, INTENTS, LOCK, SOCKET, MAX_BYTES, atomic_json, config_lock, config_revision
from strategy_config import (normalize_configuration, read_configuration, automatic_rows, new_manual_row,
                             add_pair, remove_pair, pair_partner,
                             set_automatic_sides, keep_saved_run_flags, requested_sides, guard_automatic_edit)

REMOVALS = BASE / "remove_after_exit.json"   # manual threads to delete once they finish
PAIR_STATE = BASE / "pair_state.json"   # {"date", "cooling": {name: {"since", "partner"}}, "last_exit": {name: epoch}}


def _today():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()


def pair_state():
    try:
        st = json.loads(PAIR_STATE.read_text())
    except (OSError, ValueError):
        st = None
    if not isinstance(st, dict) or st.get("date") != _today():
        st = {"date": _today(), "cooling": {}, "last_exit": {}}
    return st


def contract_label(row):
    return f"{row.get('strike_price')} {row.get('option_type')}" if row else "the paired thread"


STOPPED_AFTER_SL = BASE / "stopped_after_sl.json"   # {"date": ..., "threads": {name: {"at", "symbol"}}}


def stopped_after_sl_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        data = json.loads(STOPPED_AFTER_SL.read_text())
    except (OSError, ValueError):
        return {}
    return data.get("threads", {}) if data.get("date") == datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat() else {}


def _record_stopped_after_sl(name, at, symbol):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    threads = stopped_after_sl_today()
    threads[name] = {"at": at, "symbol": symbol}
    atomic_json(STOPPED_AFTER_SL, {"date": datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat(), "threads": threads})


def sl_mail_html(row, exp_text, entry, exit_price, qty, pnl, at, note="This thread has been stopped and deleted."):
    from mail_card import card, money
    price = lambda v: f"₹{v:,.2f}" if v else "see Kotak"
    return card("STOP-LOSS HIT", "red", f"{row.get('strike_price')} {row.get('option_type')}",
                f"{row.get('underlying')} · {exp_text} expiry",
                big=money(pnl) if pnl is not None else None, big_tone="red" if (pnl or 0) < 0 else "green",
                rows=[("Bought at", price(entry)), ("Sold at", price(exit_price)), ("Quantity", str(qty or "—")),
                      ("Time", f"{str(at)[11:16]} IST")],
                note=note)


def max_loss_mail_html(snapshot, names):
    from mail_card import card, money, tone_of
    pos = [[p["symbol"], str(p["qty"]), f"{p['ltp']}", (money(p["pnl"]), tone_of(p["pnl"]))] for p in snapshot["positions"]]
    return card("MAX LOSS HIT", "red", "Trading stopped for today", f"Hit at {snapshot['tripped_at'][11:16]} IST",
                big=money(snapshot["pnl"]), big_tone="red",
                rows=[("Max loss limit", money(-snapshot["trip_limit"])), ("Booked", money(snapshot["realized"])),
                      ("Open", money(snapshot["unrealized"])), ("Threads turned off", ", ".join(names) or "none")],
                table=(["SELLING NOW", "QTY", "LTP", "P&L"], pos) if pos else None,
                note="Open positions are being sold at market. Nothing can start again until the next trading day. "
                     "You'll get one more email when everything is closed.")


def closed_mail_html(snapshot):
    from mail_card import card, money
    return card("ALL CLOSED", "grey", "Every position is closed", "All SRSI threads are off for today",
                big=money(snapshot["realized"]), big_tone="red" if snapshot["realized"] < 0 else "green",
                rows=[("Booked today (before charges)", money(snapshot["realized"])),
                      ("Max loss limit", money(-snapshot["trip_limit"]))],
                note="Trading can start again on the next trading day.")


class WorkerSupervisor:
    def __init__(self, factory):
        self.factory = factory
        self.records = {}
        self.guard = threading.RLock()
        self.closing = threading.Event()
        self.intents = json.loads(INTENTS.read_text()) if INTENTS.exists() else {}
        self.removals = set(json.loads(REMOVALS.read_text())) if REMOVALS.exists() else set()
        self.pairs = pair_state()
        self._pairs_checked = 0.0
        self.max_loss = None           # latest snapshot of today's P&L against the max loss
        self._max_loss_checked = 0.0

    def _persist(self):
        atomic_json(INTENTS, self.intents)

    def _save_removals(self):
        atomic_json(REMOVALS, sorted(self.removals))

    def _delete_row(self, name):
        with config_lock():
            config = read_configuration(CONFIG)
            config["workers"] = [r for r in config["workers"] if r["name"] != name]
            atomic_json(CONFIG, normalize_configuration(config))
        self.removals.discard(name)
        self._save_removals()

    def _set_enabled(self, name, enabled):
        with config_lock():
            config = read_configuration(CONFIG)
            row = next((r for r in config["workers"] if r["name"] == name), None)
            if row is None:
                raise ValueError("Saved worker was removed")
            row["enabled"] = enabled
            atomic_json(CONFIG, normalize_configuration(config))

    def _collect(self):
        # Restart is allowed only after join/disconnect and confirmed flat state.
        for name, record in list(self.records.items()):
            thread, worker = record["thread"], record["worker"]
            if not thread.is_alive() and worker.runtime_retired_safely:
                thread.join()
                if getattr(worker, "stopped_after_sl", None):
                    leg = worker.child if getattr(worker, "child", None) else worker
                    _record_stopped_after_sl(name, worker.stopped_after_sl, getattr(leg, "symbol", None))
                if not record["configuration"].get("automatic"):
                    if getattr(worker, "stopped_after_sl", None):
                        # Manual thread hit its stop-loss: delete it and send a short alert.
                        self._delete_row(name)
                        self._mail_sl(record["configuration"], getattr(worker, "sl_trade", {}) or {}, worker.stopped_after_sl)
                    elif name in self.removals:
                        self._delete_row(name)
                    else:
                        self._set_enabled(name, False)
                elif worker.runtime_pause.is_set():
                    with config_lock():
                        config = read_configuration(CONFIG)
                        set_automatic_sides(config, [record["configuration"]["option_type"]], False)
                        atomic_json(CONFIG, normalize_configuration(config))
                self.intents.pop(name, None)
                self._persist()
                del self.records[name]

    def status(self):
        self._collect()
        workers = []
        for name, record in self.records.items():
            w = record["worker"]
            state = "halted" if w.state.halted or not record["thread"].is_alive() else (
                "stopping" if w.runtime_pause.is_set() else "running" if w.runtime_ready else "starting")
            workers.append({"name": name, "state": state, "phase": w.state.phase,
                            "quantity": w.state.position_quantity,
                            "pending_order": w.state.pending is not None,
                            "position_sync_blocked": bool(getattr(w.child if hasattr(w, "child") and w.child else w, "position_sync_blocked", False)),
                            "stop_after_exit": w.runtime_pause.is_set(),
                            "configuration": record["configuration"],
                            "selection": w.runtime_details() if hasattr(w, "runtime_details") else None,
                            "leg": self._leg_info(w) if not record["configuration"].get("automatic") else None,
                            "removing": name in self.removals,
                            "cooldown": getattr(w, "cooldown", None),
                            "pair_partner": getattr(w, "pair_partner", None)})
        config = read_configuration(CONFIG)
        return {"online": True, "pid": os.getpid(), "workers": workers, "max_loss": self.max_loss,
                "stopped_after_sl": stopped_after_sl_today(),
                "pair_cooling": self.pairs.get("cooling", {}),
                "mode": config["mode"], "automatic": config["automatic"], "start_errors": getattr(self, "start_errors", {}),
                "saved_workers": config["workers"], "config_revision": config_revision(config),
                "observed_at": time.monotonic_ns()}

    @staticmethod
    def _leg_info(w):
        st = w.state
        k = getattr(st, "last_k", None)
        entry = getattr(st, "entry_average_price", 0.0) or None
        return {"symbol": getattr(w, "symbol", None), "ltp": getattr(st, "last_ltp", None),
                "k": k if isinstance(k, (int, float)) and k == k else None, "entry_price": entry,
                "stop_price": entry - w.stop_loss_points if entry and hasattr(w, "stop_loss_points") else None,
                "order_quantity": getattr(w, "order_quantity", 0) or None, "ready": bool(w.runtime_ready)}

    def _start(self, name, restoring=False):
        if self.closing.is_set():
            raise ValueError("Strategy process is stopping")
        self._collect()
        if name in self.records:
            raise ValueError("This thread is already active or requires order reconciliation")
        # After the max loss is hit only recovery starts are allowed (they sell what is held).
        if max_loss.is_tripped() and not (restoring and name in self.intents):
            raise ValueError("Max loss for today was reached; trading is off until the next trading day")
        with config_lock():
            config = read_configuration(CONFIG)
            candidates = automatic_rows(config) if config["mode"] == "automatic" else config["workers"]
            row = next((r for r in candidates if r["name"] == name), None)
            if row is None:
                raise ValueError("This thread does not belong to the selected mode")
            if config["mode"] == "automatic" and len(self.records) >= 2:
                raise ValueError("Automatic mode supports at most two active threads")
            row = copy.deepcopy(row)
            for record in self.records.values():
                other = record["configuration"]
                keys = ("underlying", "expiry_date", "strike_price", "option_type")
                if all(row[k] == other[k] for k in keys):
                    raise ValueError("Another active thread owns this option contract")
            if name in self.intents and {**row, "enabled": True} != {**self.intents[name], "enabled": True}:
                raise ValueError("Pending exit configuration changed; restore it before starting")
            row["enabled"] = True
            worker = self.factory(row, copy.deepcopy(config["settings"]))
            if not row.get("automatic"):
                self._apply_pair(name, worker, config)
            if row.get("automatic"):
                set_automatic_sides(config, [row["option_type"]], True)
            else:
                config["workers"] = [row if r["name"] == name else r for r in config["workers"]]
            atomic_json(CONFIG, normalize_configuration(config))
        if name in self.intents:
            worker.runtime_pause.set()
        thread = threading.Thread(target=self._run_worker, args=(worker,),
                                  name=f"{name}-{row['option_type']}-worker", daemon=True)
        self.records[name] = {"thread": thread, "worker": worker, "configuration": row}
        try:
            thread.start()
        except Exception:
            worker.state.halted = True
            raise

    @staticmethod
    def _run_worker(worker):
        try:
            worker.run()
        except Exception:
            worker.state.halted = True
            worker.logger.exception("Worker ended unexpectedly; runtime restart is blocked")

    def command(self, request):
        with self.guard:
            action = request.get("action")
            self._collect()
            if action == "status":
                return self.status()
            if action == "save":
                config = normalize_configuration(request.get("configuration"))
                current_config = read_configuration(CONFIG)
                if config["mode"] != current_config["mode"]:
                    raise ValueError("Use the mode switch to change modes safely")
                # Run flags change only through Start/Stop; keep the saved ones.
                keep_saved_run_flags(config, current_config)
                guard_automatic_edit(config, current_config)
                rows = {r["name"]: r for r in (automatic_rows(config) if config["mode"] == "automatic" else config["workers"])}
                for name, record in self.records.items():
                    if rows.get(name) != record["configuration"]:
                        raise ValueError(f"Stop {name} and wait for it to finish before editing or removing it")
                with config_lock():
                    expected = request.get("revision")
                    if expected and expected != config_revision(read_configuration(CONFIG)):
                        raise ValueError("Configuration changed; reload saved settings before saving")
                    atomic_json(CONFIG, config)
                return {"configuration": config}
            if action == "set_mode":
                target = request.get("mode")
                if target not in {"manual", "automatic"}:
                    raise ValueError("Choose Manual or Automatic mode")
                config = read_configuration(CONFIG)
                if config["mode"] == target:
                    return self.status()
                # The same lock surrounds signal decisions, closing the check/start race.
                with ExitStack() as locks:
                    for record in self.records.values():
                        locks.enter_context(record["worker"].runtime_state_lock)
                    for name, record in self.records.items():
                        w = record["worker"]
                        if (not w.runtime_ready or w.state.halted or w.state.phase != "WAITING_TO_BUY"
                                or w.state.position_quantity or w.state.pending is not None):
                            raise ValueError(f"{name} must be waiting to buy with no position or pending order before switching modes")
                    for record in self.records.values():
                        record["worker"].runtime_pause.set()
                for record in self.records.values():
                    record["thread"].join(timeout=3)
                self._collect()
                if self.records:
                    raise ValueError("Flat threads are finishing shutdown; retry the mode switch shortly")
                from automatic_worker import journal_entries
                if journal_entries() or self.intents:
                    raise ValueError("Recover pending contracts before changing modes")
                with config_lock():
                    config = read_configuration(CONFIG)
                    config["mode"] = target
                    set_automatic_sides(config, ["CE", "PE"], False)
                    atomic_json(CONFIG, normalize_configuration(config))
                return self.status()
            if action == "automatic_start":
                config = read_configuration(CONFIG)
                if config["mode"] != "automatic":
                    raise ValueError("Select Automatic mode first")
                sides = requested_sides(request.get("sides"))
                names = ["AUTO-" + side for side in sides]
                capture = request.get("capture")
                # Only the requested sides need to be clear; the other side may keep running or draining.
                for name in names:
                    record = self.records.get(name)
                    if name in self.intents or (record and (record["worker"].runtime_pause.is_set() or record["worker"].state.halted)):
                        raise ValueError(f"{name} is finishing an exit or needs reconciliation; wait before starting it")
                if capture is not None:
                    # The mode can change only while the side is off, and is saved before the worker starts.
                    if any(name in self.records for name in names):
                        raise ValueError("Turn the side off before changing its mode")
                    from automatic_worker import journal_entries
                    pending = journal_entries()
                    if any(name in pending and config["automatic"]["capture_" + name[-2:].lower()] != capture for name in names):
                        raise ValueError("Recover this side's contracts before changing its mode")
                    with config_lock():
                        config = read_configuration(CONFIG)
                        # Off until _start turns each requested side on below.
                        set_automatic_sides(config, sides, False, capture=capture)
                        atomic_json(CONFIG, normalize_configuration(config))
                    config = read_configuration(CONFIG)
                for row in automatic_rows(config):
                    if row["name"] in names and row["name"] not in self.records:
                        self._start(row["name"])
                return self.status()
            if action == "automatic_stop":
                config = read_configuration(CONFIG)
                if config["mode"] != "automatic":
                    raise ValueError("Select Automatic mode first")
                sides = requested_sides(request.get("sides"))
                names = {"AUTO-" + side for side in sides}
                for name, record in self.records.items():
                    if name in names and record["configuration"].get("automatic"):
                        self.intents[name] = record["configuration"]
                        record["worker"].runtime_pause.set()
                        record["worker"].logger.info("[STOP AFTER EXIT] New entries disabled; existing exit rules continue")
                self._persist()
                with config_lock():
                    config = read_configuration(CONFIG)
                    set_automatic_sides(config, sides, False)
                    atomic_json(CONFIG, normalize_configuration(config))
                return self.status()
            if action == "add_start":
                # Quick add: save the contract and start its thread in one step.
                config = read_configuration(CONFIG)
                if config["mode"] != "manual":
                    raise ValueError("Switch to Manual mode to add a thread")
                with config_lock():
                    config = read_configuration(CONFIG)
                    row = new_manual_row(config, request.get("row"))
                    config["workers"].append(row)
                    atomic_json(CONFIG, normalize_configuration(config))
                try:
                    self._start(row["name"])
                except Exception:
                    with config_lock():
                        config = read_configuration(CONFIG)
                        config["workers"] = [r for r in config["workers"] if r["name"] != row["name"]]
                        atomic_json(CONFIG, normalize_configuration(config))
                    raise
                return {**self.status(), "added": row["name"]}
            if action in ("pair", "unpair"):
                with config_lock():
                    config = read_configuration(CONFIG)
                    if action == "pair":
                        names = [str(request.get("ce", "")), str(request.get("pe", ""))]
                        self._pair_change_allowed(names)
                        add_pair(config, *names)
                    else:
                        name = str(request.get("name", ""))
                        partner = pair_partner(config, name)
                        if not partner:
                            raise ValueError("This thread is not paired")
                        self._pair_change_allowed([name, partner])
                        remove_pair(config, name)
                    atomic_json(CONFIG, normalize_configuration(config))
                self._pairs_checked = 0.0
                try:
                    self._check_pairs()
                except Exception as exc:
                    print(f"[PAIRS] check failed: {exc}", flush=True)
                return self.status()
            if action == "remove":
                # Off = delete. A flat thread goes now; a held one keeps its exit rules and goes after selling.
                name = str(request.get("name", ""))
                config = read_configuration(CONFIG)
                if not any(r["name"] == name for r in config["workers"]):
                    raise ValueError("This thread no longer exists")
                record = self.records.get(name)
                if record is None and name not in self.intents:
                    self._delete_row(name)
                    return self.status()
                self.removals.add(name)
                self._save_removals()
                if record is not None:
                    self.intents[name] = record["configuration"]
                    self._persist()
                    record["worker"].runtime_pause.set()
                    record["worker"].logger.info("[REMOVE AFTER EXIT] New entries disabled; deleted once flat")
                self._set_enabled(name, False)
                return self.status()
            name = str(request.get("name", ""))
            if action == "start":
                self._start(name)
            elif action == "stop_after_exit":
                record = self.records.get(name)
                if record is None:
                    self._set_enabled(name, False)
                else:
                    self.intents[name] = record["configuration"]
                    self._persist()
                    record["worker"].runtime_pause.set()
                    record["worker"].logger.info("[STOP AFTER EXIT] New entries disabled; existing exit rules continue")
            else:
                raise ValueError("Unknown runtime action")
            return self.status()

    def _watch_trading_mode(self):
        """Ask OpenAlgo every 15 s whether it is in Analyze or Live, so max loss counts each separately."""
        from urllib.request import Request, urlopen
        while not self.closing.is_set():
            try:
                body = json.dumps({"apikey": os.getenv("OPENALGO_API_KEY", "")}).encode()
                with urlopen(Request("http://127.0.0.1:5000/api/v1/analyzer", data=body, method="POST",
                                     headers={"Content-Type": "application/json"}), timeout=10) as r:
                    data = json.loads(r.read()).get("data") or {}
                analyze = data.get("analyze_mode")
                if analyze is None and data.get("mode") in ("analyze", "live"):
                    analyze = data["mode"] == "analyze"
                if analyze is not None:
                    max_loss.set_mode("analyze" if analyze else "live")
            except Exception:
                pass
            self.closing.wait(15)

    def _pair_change_allowed(self, names):
        """Pairs change only while neither thread holds a position, has an order working or is cooling down."""
        for name in names:
            record = self.records.get(name)
            if record is not None:
                w = record["worker"]
                if w.state.position_quantity or w.state.pending is not None or w.state.phase != "WAITING_TO_BUY":
                    raise ValueError(f"{name} is holding a position or has an order working; pair it when it is flat")
            if name in self.pairs.get("cooling", {}):
                raise ValueError(f"{name} is cooling down after a stop-loss; wait until it is free")

    def _apply_pair(self, name, worker, config):
        partner = pair_partner(config, name)
        worker.pair_partner = partner
        cool = self.pairs.get("cooling", {}).get(name)
        if partner and cool:
            worker.cooldown = f"Cooling down · waiting for {cool.get('partner_label', partner)} to finish a trade"
            worker.cooldown_since = cool["since"]

    def _save_pairs(self):
        atomic_json(PAIR_STATE, self.pairs)

    def _check_pairs(self):
        """Paired Manual threads: start a cooldown after a stop-loss, end it once the partner finishes a trade."""
        now = time.monotonic()
        if now - self._pairs_checked < 0.5:
            return
        self._pairs_checked = now
        if self.pairs.get("date") != _today():
            self.pairs = pair_state()
        config = read_configuration(CONFIG)
        rows = {r["name"]: r for r in config["workers"]}
        changed = False
        for name, record in self.records.items():
            if record["configuration"].get("automatic"):
                continue
            w = record["worker"]
            w.pair_partner = pair_partner(config, name)
            last_exit = getattr(w, "last_exit_at", None)
            if last_exit and last_exit > self.pairs["last_exit"].get(name, 0):
                self.pairs["last_exit"][name] = last_exit
                changed = True
            if getattr(w, "cooldown", None) and name not in self.pairs["cooling"] and w.pair_partner:
                partner_row = rows.get(w.pair_partner)
                self.pairs["cooling"][name] = {"since": getattr(w, "cooldown_since", None) or time.time(), "partner": w.pair_partner,
                                               "partner_label": contract_label(partner_row)}
                w.cooldown = f"Cooling down · waiting for {contract_label(partner_row)} to finish a trade"
                changed = True
                self._mail_sl(record["configuration"], getattr(w, "sl_trade", {}) or {},
                              getattr(w, "sl_cooldown_at", ""),
                              note=f"Paired with {contract_label(partner_row)}: this side waits until "
                                   f"{contract_label(partner_row)} finishes one trade, then trades again. Not deleted.")
        for name, cool in list(self.pairs["cooling"].items()):
            partner = pair_partner(config, name)
            finished = self.pairs["last_exit"].get(cool["partner"], 0) > cool["since"]
            if not partner or partner != cool["partner"] or finished or name not in rows:
                del self.pairs["cooling"][name]
                changed = True
                record = self.records.get(name)
                if record is not None:
                    record["worker"].cooldown = None
                    record["worker"].logger.info("[PAIR COOLDOWN END] %s may trade again (%s)", name,
                                                 "partner finished a trade" if finished else "pair removed")
        if changed:
            self._save_pairs()

    @staticmethod
    def _mail_sl(row, trade, at, note="This thread has been stopped and deleted."):
        months = {"JAN": "Jan", "FEB": "Feb", "MAR": "Mar", "APR": "Apr", "MAY": "May", "JUN": "Jun", "JUL": "Jul",
                  "AUG": "Aug", "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec"}
        exp = row.get("expiry_date", "")
        exp_text = f"{int(exp[:2])} {months.get(exp[2:5], exp[2:5])}" if len(exp) == 7 else exp
        contract = f"{row.get('underlying')} {row.get('strike_price')} {row.get('option_type')} · {exp_text} expiry"
        entry, exit_price, qty = float(trade.get("entry") or 0), float(trade.get("exit") or 0), int(trade.get("qty") or 0)
        pnl = (exit_price - entry) * qty if entry and exit_price and qty else None
        money = lambda v: ("-" if v < 0 else "+") + f"Rs {abs(v):,.2f}"
        lines = [f"{contract} hit its stop-loss.", "",
                 f"Bought at {entry:.2f}" if entry else "Bought at: see Kotak",
                 f"Sold at   {exit_price:.2f}" if exit_price else "Sold at: see Kotak",
                 f"Qty {qty}   P&L {money(pnl)}" if pnl is not None else "",
                 f"Time {str(at)[11:16]} IST", "", note]
        try:
            max_loss.send_alert(f"sl-{row.get('name')}-{str(at)[11:19]}",
                                f"SL hit: {row.get('strike_price')} {row.get('option_type')} {exp_text}" + (f" · {money(pnl)}" if pnl is not None else ""),
                                "\n".join(l for l in lines if l is not None),
                                html=sl_mail_html(row, exp_text, entry, exit_price, qty, pnl, at, note=note))
        except Exception as exc:
            print(f"[SL MAIL] could not send: {exc}", flush=True)

    def _check_max_loss(self):
        now = time.monotonic()
        if now - self._max_loss_checked < 0.5:
            return
        self._max_loss_checked = now
        try:
            snapshot, just_tripped = max_loss.evaluate([r["worker"] for r in self.records.values()])
        except Exception as exc:
            print(f"[MAX LOSS] check failed: {exc}", flush=True)
            return
        self.max_loss = snapshot
        if just_tripped:
            self._trip_max_loss(snapshot)
        elif snapshot["tripped"] and not self.records and not snapshot["closed_at"] and max_loss.mark_closed():
            max_loss.send_alert("closed", "SRSI: all positions closed after max loss",
                                "Every position is now closed and all SRSI threads are off for today.\n\n"
                                f"Today's P&L: {max_loss.rupees(snapshot['realized'])} (booked, before charges)\n"
                                f"Max loss limit: {max_loss.rupees(-snapshot['trip_limit'])}\n\n"
                                "Trading can start again on the next trading day.", html=closed_mail_html(snapshot))

    def _trip_max_loss(self, snapshot):
        """Stop every thread: no new buys, sell what is held (legs do that themselves), all off for today."""
        names = []
        for name, record in self.records.items():
            names.append(name)
            self.intents[name] = record["configuration"]
            record["worker"].runtime_pause.set()
            record["worker"].logger.warning("[MAX LOSS] Today's P&L %.2f reached the max loss %.0f; selling and turning off",
                                            snapshot["pnl"], snapshot["trip_limit"])
        self._persist()
        try:
            with config_lock():
                config = read_configuration(CONFIG)
                set_automatic_sides(config, ["CE", "PE"], False)
                for row in config["workers"]:
                    row["enabled"] = False
                atomic_json(CONFIG, normalize_configuration(config))
        except Exception as exc:
            print(f"[MAX LOSS] could not turn saved threads off: {exc}", flush=True)
        print(f"[MAX LOSS] tripped pnl={snapshot['pnl']} limit={snapshot['trip_limit']} threads={names}", flush=True)
        lines = [f"Today's loss reached your max loss, so SRSI is selling everything and turning off for today.",
                 "",
                 f"Time: {snapshot['tripped_at'][:19].replace('T', ' ')} IST",
                 f"Today's P&L: {max_loss.rupees(snapshot['pnl'])}  (booked {max_loss.rupees(snapshot['realized'])}, open {max_loss.rupees(snapshot['unrealized'])}, before charges)",
                 f"Max loss limit: {max_loss.rupees(-snapshot['trip_limit'])}",
                 ""]
        if snapshot["positions"]:
            lines.append("Selling at market now:")
            for row in snapshot["positions"]:
                note = "  ⚠ halted, check it in OpenAlgo" if row["halted"] else ""
                lines.append(f"  • {row['symbol']}  qty {row['qty']}  LTP {row['ltp']}  P&L {max_loss.rupees(row['pnl'])}{note}")
        else:
            lines.append("No positions were open.")
        lines += ["", "Threads turned off: " + (", ".join(names) if names else "none were running"),
                  "Nothing can start again until the next trading day. You'll get another email when every position is closed."]
        max_loss.send_alert("tripped", f"SRSI max loss hit: {max_loss.rupees(snapshot['pnl'])} today", "\n".join(lines),
                            html=max_loss_mail_html(snapshot, names))

    def run(self):
        lock = LOCK.open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("SRSI worker supervisor is already running") from None
        supervisor = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(10)
                try:
                    raw = self.rfile.readline(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
                        raise ValueError("Invalid runtime request")
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        raise ValueError("Runtime request must be an object")
                    result = supervisor.command(request)
                except Exception as exc:
                    result = {"error": str(exc)}
                self.wfile.write(json.dumps(result).encode() + b"\n")

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        server = None
        old_signals = {}
        try:
            SOCKET.unlink(missing_ok=True)
            server = Server(str(SOCKET), Handler)
            os.chmod(SOCKET, 0o600)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            # Start normal configured workers once. Later starts are explicit UI actions.
            with self.guard:
                config = read_configuration(CONFIG)
                from automatic_worker import journal_entries
                recovering = journal_entries()
                self.start_errors = {}
                candidates = automatic_rows(config) if config["mode"] == "automatic" else config["workers"]
                if max_loss.is_tripped():
                    for row in candidates:
                        if row["name"] in recovering and row["name"] not in self.intents:
                            self.intents[row["name"]] = row
                    self._persist()
                for row in candidates:
                    if row["enabled"] or row["name"] in self.intents or row["name"] in recovering:
                        try:
                            self._start(row["name"], restoring=True)
                        except Exception as exc:
                            self.start_errors[row["name"]] = str(exc)
                            print(f"[RUNTIME START ERROR] {row['name']}: {exc}", flush=True)
            threading.Thread(target=self._watch_trading_mode, name="trading-mode", daemon=True).start()
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGTERM, signal.SIGINT):
                    old_signals[sig] = signal.signal(sig, lambda *_: self.closing.set())
            print("[RUNTIME] Thread controls ready; supervisor remains available when all threads are off", flush=True)
            while not self.closing.wait(.25):
                with self.guard:
                    self._collect()
                    self._check_max_loss()
                    try:
                        self._check_pairs()
                    except Exception as exc:   # pairing must never stop the supervisor
                        print(f"[PAIRS] check failed: {exc}", flush=True)
        finally:
            self.closing.set()
            if server:
                server.shutdown()
                server.server_close()
            with self.guard:
                for record in self.records.values():
                    record["worker"].stop()
                for record in self.records.values():
                    record["thread"].join(timeout=5)
            SOCKET.unlink(missing_ok=True)
            for sig, handler in old_signals.items():
                signal.signal(sig, handler)
            lock.close()
