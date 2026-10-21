"""Local IPC and atomic config storage for the SRSI worker supervisor."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from strategy_config import (normalize_configuration, read_configuration, automatic_rows,
                             set_automatic_sides, keep_saved_run_flags, requested_sides, guard_automatic_edit)

BASE = Path(__file__).resolve().parent
SOCKET = BASE / "runtime.sock"
LOCK = BASE / "runtime.lock"
CONFIG = BASE / "workers.json"
INTENTS = BASE / "runtime_stop_intents.json"
_file_lock = threading.RLock()
MAX_BYTES = 128 * 1024


def config_revision(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def config_lock():
    with _file_lock, (BASE / "workers.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def process_locked():
    with LOCK.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
        return False


def runtime_request(value):
    """An absent socket means offline only if the process lock is also free."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        try:
            client.connect(str(SOCKET))
        except OSError:
            if process_locked():
                raise RuntimeError("Thread controller is unavailable; refresh before making changes")
            return None
        client.sendall(json.dumps(value).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data:
            block = client.recv(4096)
            if not block:
                raise RuntimeError("Thread controller disconnected")
            data.extend(block)
            if len(data) > MAX_BYTES:
                raise RuntimeError("Thread controller response is too large")
    result = json.loads(bytes(data).split(b"\n", 1)[0])
    if "error" in result:
        raise ValueError(result["error"])
    return result


def offline_status():
    intents = json.loads(INTENTS.read_text()) if INTENTS.exists() else {}
    config = read_configuration(CONFIG)
    from automatic_worker import journal_entries
    recovery = journal_entries()
    workers = [{"name": name, "state": "waiting_for_strategy", "stop_after_exit": True,
                "configuration": row, "quantity": None, "pending_order": True}
               for name, row in intents.items()]
    workers += [{"name": name, "state": "waiting_for_strategy", "stop_after_exit": False,
                 "configuration": saved["configuration"], "quantity": None, "pending_order": True,
                 "selection": {"symbol": None, "status": "Recovery pending · start SRSI"}}
                for name, saved in recovery.items() if name not in intents]
    return {"online": False, "workers": workers, "saved_workers": config["workers"],
            "mode": config["mode"], "automatic": config["automatic"],
            "config_revision": config_revision(config), "observed_at": time.monotonic_ns(),
            "message": "Start SRSI in Python Strategies to use runtime controls.",
            "stopped_after_sl": __import__("worker_supervisor").stopped_after_sl_today(),
            "pair_cooling": __import__("worker_supervisor").pair_state().get("cooling", {})}


def save_config(value):
    normalized = normalize_configuration(value)
    result = runtime_request({"action": "save", "configuration": normalized, "revision": value.get("revision") if isinstance(value, dict) else None})
    if result is not None:
        return result["configuration"]
    with config_lock():
        if process_locked():
            raise RuntimeError("Strategy is starting; refresh before saving")
        revision = value.get("revision") if isinstance(value, dict) else None
        if revision and revision != config_revision(read_configuration(CONFIG)):
            raise ValueError("Configuration changed; reload saved settings before saving")
        # Pending exits survive process restarts and cannot be edited away.
        intents = json.loads(INTENTS.read_text()) if INTENTS.exists() else {}
        current = read_configuration(CONFIG)
        if normalized["mode"] != current["mode"]:
            raise ValueError("Use the mode switch to change modes safely")
        # Run flags change only through the toggles; keep the saved ones.
        keep_saved_run_flags(normalized, current)
        guard_automatic_edit(normalized, current)
        from automatic_worker import journal_entries
        recovery = journal_entries()
        if recovery and (normalized["automatic"] != current["automatic"] or normalized["settings"] != current["settings"]):
            raise ValueError("Recover automatic contracts before editing their settings")
        rows = {row["name"]: row for row in (automatic_rows(normalized) if normalized["mode"] == "automatic" else normalized["workers"])}
        for name, frozen in intents.items():
            if name not in rows or {**rows[name], "enabled": True} != {**frozen, "enabled": True}:
                raise ValueError(f"{name} is waiting to finish its exit; keep its saved configuration")
        atomic_json(CONFIG, normalized)
    return normalized


def switch_mode_offline(target):
    if target not in {"manual", "automatic"}:
        raise ValueError("Choose Manual or Automatic mode")
    with config_lock():
        if process_locked():
            raise ValueError("Strategy is starting; retry shortly")
        from automatic_worker import journal_entries
        if journal_entries() or (INTENTS.exists() and json.loads(INTENTS.read_text())):
            raise ValueError("Start SRSI and reconcile pending trades before switching modes")
        config = read_configuration(CONFIG)
        if config["mode"] == "manual" and any(row["enabled"] for row in config["workers"]):
            raise ValueError("Start SRSI and stop enabled manual threads before switching modes")
        config["mode"] = target
        set_automatic_sides(config, ["CE", "PE"], False)
        atomic_json(CONFIG, normalize_configuration(config))
    return offline_status()


def arm_automatic_offline(sides, enabled, capture=None):
    """While SRSI is stopped, save which automatic sides (and modes) start at the next SRSI start."""
    sides = requested_sides(sides)
    with config_lock():
        if process_locked():
            raise ValueError("Strategy is starting; retry shortly")
        config = read_configuration(CONFIG)
        if config["mode"] != "automatic":
            raise ValueError("Select Automatic mode first")
        from automatic_worker import journal_entries
        if capture is not None and any("AUTO-" + side in journal_entries() for side in sides):
            raise ValueError("Recover this side's contracts before changing its mode")
        set_automatic_sides(config, sides, enabled, capture=capture)
        atomic_json(CONFIG, normalize_configuration(config))
    return offline_status()


REMOVALS = BASE / "remove_after_exit.json"


def add_manual_offline(value):
    """SRSI stopped: save the new thread switched on, so it starts at the next SRSI start."""
    from strategy_config import new_manual_row
    with config_lock():
        if process_locked():
            raise ValueError("Strategy is starting; retry shortly")
        config = read_configuration(CONFIG)
        if config["mode"] != "manual":
            raise ValueError("Switch to Manual mode to add a thread")
        row = new_manual_row(config, value)
        row["enabled"] = True
        config["workers"].append(row)
        atomic_json(CONFIG, normalize_configuration(config))
    return {**offline_status(), "added": row["name"]}


def remove_manual_offline(name):
    """SRSI stopped: delete the thread now, or after its pending exit if one is waiting."""
    with config_lock():
        if process_locked():
            raise ValueError("Strategy is starting; retry shortly")
        config = read_configuration(CONFIG)
        if not any(r["name"] == name for r in config["workers"]):
            raise ValueError("This thread no longer exists")
        intents = json.loads(INTENTS.read_text()) if INTENTS.exists() else {}
        if name in intents:
            removals = set(json.loads(REMOVALS.read_text())) if REMOVALS.exists() else set()
            atomic_json(REMOVALS, sorted(removals | {name}))
            for row in config["workers"]:
                if row["name"] == name:
                    row["enabled"] = False
        else:
            config["workers"] = [r for r in config["workers"] if r["name"] != name]
        atomic_json(CONFIG, normalize_configuration(config))
    return offline_status()


def arm_manual_offline(name):
    """SRSI stopped: switch a saved manual thread on, so it starts at the next SRSI start."""
    with config_lock():
        if process_locked():
            raise ValueError("Strategy is starting; retry shortly")
        config = read_configuration(CONFIG)
        if config["mode"] != "manual":
            raise ValueError("Switch to Manual mode first")
        row = next((r for r in config["workers"] if r["name"] == name), None)
        if row is None:
            raise ValueError("This thread no longer exists")
        row["enabled"] = True
        atomic_json(CONFIG, normalize_configuration(config))
    return offline_status()


def pair_offline(action, value):
    """SRSI stopped: pair or unpair Manual threads in the saved config."""
    from strategy_config import add_pair, remove_pair, pair_partner
    import worker_supervisor
    with config_lock():
        if process_locked():
            raise ValueError("Strategy is starting; retry shortly")
        config = read_configuration(CONFIG)
        cooling = worker_supervisor.pair_state().get("cooling", {})
        if action == "pair":
            names = [str(value.get("ce", "")), str(value.get("pe", ""))]
            if any(n in cooling for n in names):
                raise ValueError("One of these threads is cooling down after a stop-loss")
            add_pair(config, *names)
        else:
            name = str(value.get("name", ""))
            if not pair_partner(config, name):
                raise ValueError("This thread is not paired")
            if name in cooling or pair_partner(config, name) in cooling:
                raise ValueError("This pair is cooling down after a stop-loss; wait until it is free")
            remove_pair(config, name)
        atomic_json(CONFIG, normalize_configuration(config))
    return offline_status()
