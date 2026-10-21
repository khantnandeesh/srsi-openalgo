"""Configuration form and local proxy for SRSI runtime thread controls."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from strategy_config import SUPPORTED_INDEXES, normalize_configuration, read_configuration
from runtime_control import (runtime_request, offline_status, save_config, config_revision, switch_mode_offline,
                             arm_automatic_offline, add_manual_offline, remove_manual_offline, arm_manual_offline, pair_offline)
import max_loss


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT_DIR / "workers.json"
PAGE_FILE = PROJECT_DIR / "config.html"
MAX_BODY_BYTES = 64 * 1024


def load_env_file(path: Path = PROJECT_DIR / ".env") -> None:
    """Load server-only credentials without returning them to the browser."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


load_env_file()
OPENALGO_HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000").rstrip("/")
OPTION_CHAIN_CACHE_TTL_SECONDS = 60.0
OPTION_CHAIN_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
OPTION_CHAIN_CACHE_LOCK = threading.Lock()


CHAIN_DEMAND: dict[tuple[str, str], float] = {}     # (index, expiry) -> last time a page asked for it
CHAIN_REFRESH_OPEN, CHAIN_REFRESH_CLOSED = 15.0, 120.0
DEMAND_KEEP_SECONDS = 20 * 60
CHAIN_FAILED: dict[tuple[str, str], float] = {}


def fetch_chain(key: tuple[str, str]) -> dict[str, Any]:
    """One option-chain request to OpenAlgo; no locks held while it runs."""
    underlying, expiry = key
    api_key = os.getenv("OPENALGO_API_KEY")
    if not api_key:
        raise RuntimeError("OPENALGO_API_KEY is not configured for the configuration service")
    request_body = json.dumps({"apikey": api_key, "underlying": underlying,
                               "exchange": SUPPORTED_INDEXES[underlying]["underlying_exchange"],
                               "expiry_date": expiry}).encode("utf-8")
    request = Request(f"{OPENALGO_HOST}/api/v1/optionchain", data=request_body,
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"OpenAlgo option-chain request failed (HTTP {exc.code})") from None
    except (URLError, TimeoutError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else "request timed out"
        raise RuntimeError(f"Could not reach OpenAlgo for the option chain: {reason}") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("OpenAlgo returned an invalid option-chain response") from None
    if not isinstance(result, dict) or result.get("status") != "success":
        message = result.get("message", "unknown API error") if isinstance(result, dict) else "invalid API response"
        raise RuntimeError(f"OpenAlgo option-chain request failed: {message}")
    with OPTION_CHAIN_CACHE_LOCK:
        OPTION_CHAIN_CACHE[key] = (time.monotonic(), result)
    return result


def chain_snapshot(key: tuple[str, str]) -> dict[str, Any]:
    """Serve the cached chain at once; the background refresher keeps it current."""
    CHAIN_DEMAND[key] = time.monotonic()
    with OPTION_CHAIN_CACHE_LOCK:
        cached = OPTION_CHAIN_CACHE.get(key)
    return cached[1] if cached else fetch_chain(key)


def _market_open_now() -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return now.weekday() < 5 and (9 * 60) <= now.hour * 60 + now.minute <= (15 * 60 + 35)


def market_refresher() -> None:
    """Fetch broker data on a timer so pages never wait on it: chains in use, and expiries."""
    try:
        config = read_configuration(CONFIG_FILE)
        now = time.monotonic()
        for underlying, expiry in {(config["automatic"]["underlying"], config["automatic"]["expiry_date"]),
                                   *((r["underlying"], r["expiry_date"]) for r in config["workers"])}:
            CHAIN_DEMAND.setdefault((underlying, expiry), now)
    except Exception:
        pass
    last_expiry = {}
    while True:
        now = time.monotonic()
        every = CHAIN_REFRESH_OPEN if _market_open_now() else CHAIN_REFRESH_CLOSED
        for key, asked in list(CHAIN_DEMAND.items()):
            if now - asked > DEMAND_KEEP_SECONDS:
                CHAIN_DEMAND.pop(key, None)
                continue
            with OPTION_CHAIN_CACHE_LOCK:
                cached = OPTION_CHAIN_CACHE.get(key)
            if cached and now - cached[0] < every:
                continue
            if now - CHAIN_FAILED.get(key, -1e9) < 60:
                continue
            try:
                fetch_chain(key)
                CHAIN_FAILED.pop(key, None)
            except Exception as exc:
                CHAIN_FAILED[key] = now
                print(f"[market-cache] {key[0]} {key[1]} refresh failed: {str(exc)[:120]}", flush=True)
        for underlying in {key[0] for key in CHAIN_DEMAND} | {"NIFTY"}:
            if now - last_expiry.get(underlying, -1e9) >= 600:
                last_expiry[underlying] = now
                try:
                    refresh_expiries(underlying)
                except Exception as exc:
                    print(f"[market-cache] {underlying} expiries failed: {str(exc)[:120]}", flush=True)
        time.sleep(3)


def get_available_strikes(query: dict[str, list[str]]) -> dict[str, Any]:
    underlying = (query.get("underlying") or [""])[0].strip().upper()
    expiry = (query.get("expiry_date") or [""])[0].strip().upper()
    option_type = (query.get("option_type") or [""])[0].strip().upper()
    if underlying not in SUPPORTED_INDEXES:
        raise ValueError("Choose a supported index")
    if not re.fullmatch(r"\d{2}[A-Z]{3}\d{2}", expiry):
        raise ValueError("Expiry must use DDMMMYY, for example 29SEP26")
    if option_type not in {"CE", "PE"}:
        raise ValueError("Option type must be CE or PE")

    api_key = os.getenv("OPENALGO_API_KEY")
    if not api_key:
        raise RuntimeError("OPENALGO_API_KEY is not configured for the configuration service")

    result = chain_snapshot((underlying, expiry))

    strikes = []
    for item in result.get("chain") or []:
        if not isinstance(item, dict):
            continue
        leg = item.get(option_type.lower())
        if not isinstance(leg, dict) or not leg.get("symbol"):
            continue
        try:
            strike = float(item["strike"])
            lot_size = int(float(leg["lotsize"]))
        except (KeyError, TypeError, ValueError):
            continue
        if strike <= 0 or lot_size <= 0:
            continue
        strikes.append(
            {
                "strike": int(strike) if strike.is_integer() else strike,
                "label": str(leg.get("label", "")),
                "symbol": str(leg["symbol"]),
                "lot_size": lot_size,
            }
        )

    strikes.sort(key=lambda item: item["strike"])
    if not strikes:
        raise RuntimeError(f"No {option_type} strikes found for {underlying} {expiry}")
    return {
        "underlying": underlying,
        "expiry_date": expiry,
        "option_type": option_type,
        "underlying_ltp": result.get("underlying_ltp"),
        "atm_strike": result.get("atm_strike"),
        "strikes": strikes,
    }



def openalgo_read_request(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("OPENALGO_API_KEY")
    if not api_key:
        raise RuntimeError("Market data is not configured for the configuration service")
    body = json.dumps({"apikey": api_key, **payload}).encode("utf-8")
    request = Request(f"{OPENALGO_HOST}/api/v1/{endpoint}", data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"OpenAlgo market-data request failed (HTTP {exc.code})") from None
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("OpenAlgo market data is temporarily unavailable") from None
    if not isinstance(result, dict) or result.get("status") != "success":
        raise RuntimeError("OpenAlgo could not return the requested market data")
    return result


EXPIRY_CACHE: dict[str, tuple[float, list[str]]] = {}
EXPIRY_CACHE_LOCK = threading.Lock()
from config_market_feed import LiveQuoteHub
LIVE_QUOTES = LiveQuoteHub(OPENALGO_HOST)


def get_expiries(query: dict[str, list[str]]) -> dict[str, Any]:
    underlying = (query.get("underlying") or [""])[0].strip().upper()
    if underlying not in SUPPORTED_INDEXES:
        raise ValueError("Choose a supported index")
    with EXPIRY_CACHE_LOCK:
        cached = EXPIRY_CACHE.get(underlying)
    dates = cached[1] if cached else refresh_expiries(underlying)
    return {"underlying": underlying, "expiries": dates}


def refresh_expiries(underlying: str) -> list[str]:
    result = openalgo_read_request("expiry", {
        "symbol": underlying,
        "exchange": SUPPORTED_INDEXES[underlying]["option_exchange"],
        "instrumenttype": "options",
    })
    dates = [
        str(value).upper().replace("-", "") for value in result.get("data", [])
        if re.fullmatch(r"\d{2}[A-Z]{3}\d{2}", str(value).upper().replace("-", ""))
    ]
    with EXPIRY_CACHE_LOCK:
        EXPIRY_CACHE[underlying] = (time.monotonic(), dates)
    return dates


def get_helper_chain(query: dict[str, list[str]]) -> dict[str, Any]:
    request_query = dict(query)
    request_query["option_type"] = ["CE"]
    metadata = get_available_strikes(request_query)
    underlying, expiry = metadata["underlying"], metadata["expiry_date"]
    with OPTION_CHAIN_CACHE_LOCK:
        raw = OPTION_CHAIN_CACHE[(underlying, expiry)][1]
        fetched_ago = time.monotonic() - OPTION_CHAIN_CACHE[(underlying, expiry)][0]
        raw = dict(raw)
    chain = []
    for item in raw.get("chain") or []:
        if not isinstance(item, dict):
            continue
        try:
            strike = int(float(item["strike"]))
        except (KeyError, TypeError, ValueError):
            continue
        row = {"strike": strike}
        for side in ("ce", "pe"):
            leg = item.get(side)
            if not isinstance(leg, dict) or not leg.get("symbol"):
                row[side] = None
                continue
            try:
                lot_size = int(float(leg.get("lotsize", 0)))
                ltp = float(leg.get("ltp", 0))
                if not math.isfinite(ltp) or ltp < 0:
                    ltp = None
            except (TypeError, ValueError):
                lot_size, ltp = 0, None
            row[side] = {
                "symbol": str(leg["symbol"]), "label": str(leg.get("label", "")),
                "lot_size": lot_size, "ltp": ltp,
            } if lot_size > 0 else None
        if row["ce"] or row["pe"]:
            chain.append(row)
    chain.sort(key=lambda row: row["strike"])
    return {
        "underlying": underlying, "expiry_date": expiry,
        "option_exchange": SUPPORTED_INDEXES[underlying]["option_exchange"],
        "underlying_exchange": SUPPORTED_INDEXES[underlying]["underlying_exchange"],
        "underlying_ltp": raw.get("underlying_ltp"),
        "atm_strike": raw.get("atm_strike"), "chain": chain,
        "snapshot_age_seconds": round(fetched_ago, 1),
    }


def helper_live_instruments(query: dict[str, list[str]]) -> list[dict[str, str]]:
    chain = get_helper_chain(query)
    try:
        center = float((query.get("center") or [chain["atm_strike"]])[0])
        if not math.isfinite(center) or center <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Choose a valid chain center strike") from None
    rows = sorted(chain["chain"], key=lambda row: abs(row["strike"] - center))[:21]
    instruments = [{"symbol": chain["underlying"], "exchange": chain["underlying_exchange"]}]
    for row in rows:
        for side in ("ce", "pe"):
            if row.get(side):
                instruments.append({"symbol": row[side]["symbol"], "exchange": chain["option_exchange"]})
    return instruments


def save_configuration(value: Any) -> dict[str, Any]:
    return save_config(value)


def runtime_payload(status):
    """Attach a coherent config snapshot; polling must not advance stale editors."""
    current = read_configuration(CONFIG_FILE)
    try:
        history = json.loads((PROJECT_DIR / "automatic_status.json").read_text())
    except (OSError, ValueError):
        history = {}
    result = {**status, "automatic_history": history}
    if "workers" in status:
        # Live numbers come from the running strategy; while it is stopped, today's saved state.
        live = status.get("max_loss") if status.get("online") else None
        result["max_loss"] = live or max_loss.offline_snapshot()
        result["max_loss"].update({k: v for k, v in max_loss.read_risk().items()})
    if status.get("config_revision") == config_revision(current):
        result["configuration"] = current
    return result


class ConfigHandler(BaseHTTPRequestHandler):
    server_version = "OpenAlgoStrategyConfig/1.0"

    def setup(self):
        super().setup()
        # Bound idle client connections so abandoned sockets cannot retain threads.
        self.connection.settimeout(30)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        if status >= 400:
            text = str(value.get("error", ""))
            category = ("configuration_conflict" if "Configuration changed" in text else
                        "worker_busy" if "before editing or removing" in text else
                        "crypto_failure" if any(token in text.lower() for token in ("sha256", "hashlib", "crypt", "fernet")) else
                        "request_rejected")
            print(f"[config-ui-error] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                  f"path={urlparse(self.path).path} status={status} category={category}", flush=True)
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API name.
        path = urlparse(self.path).path
        if path == "/api/runtime":
            try:
                self._send_json(200, runtime_payload(runtime_request({"action": "status"}) or offline_status()))
            except Exception as exc:
                self._send_json(503, {"error": str(exc)})
            return
        if path in {"/api/chain", "/api/expiries"}:
            try:
                query = parse_qs(urlparse(self.path).query)
                result = get_helper_chain(query) if path == "/api/chain" else get_expiries(query)
                self._send_json(200, runtime_payload(result))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self._send_json(502, {"error": str(exc)})
            return
        if path == "/api/live":
            viewer = None
            try:
                viewer = LIVE_QUOTES.acquire(helper_live_instruments(parse_qs(urlparse(self.path).query)))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self._send_json(503, {"error": str(exc)})
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                last_revision, last_heartbeat = -1, time.monotonic()
                while True:
                    update = LIVE_QUOTES.snapshot(viewer)
                    if update["revision"] != last_revision:
                        self.wfile.write(("data:" + json.dumps(update, separators=(",", ":")) + "\n\n").encode())
                        self.wfile.flush()
                        last_revision = update["revision"]
                        last_heartbeat = time.monotonic()
                    elif time.monotonic() - last_heartbeat >= 15:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_heartbeat = time.monotonic()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                LIVE_QUOTES.release(viewer)
            return
        if path == "/api/strikes":
            try:
                self._send_json(200, get_available_strikes(parse_qs(urlparse(self.path).query)))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self._send_json(502, {"error": str(exc)})
            return
        if path == "/api/config":
            try:
                config = read_configuration(CONFIG_FILE)
                self._send_json(200, {**config, "revision": config_revision(config)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if path in {"/", "/index.html"}:
            try:
                body = PAGE_FILE.read_bytes()
            except OSError:
                self._send(500, b"Configuration page is missing", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API name.
        path = urlparse(self.path).path
        if path == "/api/runtime":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY_BYTES:
                    raise ValueError("Invalid runtime request size")
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict) or value.get("action") not in {"start", "stop_after_exit", "set_mode", "automatic_start", "automatic_stop", "add_start", "remove", "pair", "unpair"}:
                    raise ValueError("Choose Start or Stop after exit")
                if value["action"] == "automatic_start" and value.get("capture") == "all":
                    raise ValueError("Capture all is under development")
                result = runtime_request(value)
                if result is None and value["action"] in ("pair", "unpair"):
                    self._send_json(200, runtime_payload(pair_offline(value["action"], value)))
                elif result is None and value["action"] == "add_start":
                    self._send_json(200, runtime_payload(add_manual_offline(value.get("row"))))
                elif result is None and value["action"] == "start":
                    self._send_json(200, runtime_payload(arm_manual_offline(str(value.get("name", "")))))
                elif result is None and value["action"] == "remove":
                    self._send_json(200, runtime_payload(remove_manual_offline(str(value.get("name", "")))))
                elif result is None and value["action"] == "set_mode":
                    self._send_json(200, runtime_payload(switch_mode_offline(value.get("mode"))))
                elif result is None and value["action"] in {"automatic_start", "automatic_stop"}:
                    # SRSI is stopped: save which sides start at its next start.
                    status = arm_automatic_offline(value.get("sides"), value["action"] == "automatic_start",
                                                   value.get("capture") if value["action"] == "automatic_start" else None)
                    self._send_json(200, runtime_payload(status))
                elif result is None:
                    self._send_json(409, {"error": offline_status()["message"]})
                else:
                    self._send_json(200, runtime_payload(result))
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
            except Exception as exc:
                self._send_json(503, {"error": str(exc)})
            return
        if path == "/api/risk":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY_BYTES:
                    raise ValueError("Invalid max loss request size")
                risk = max_loss.save_risk(json.loads(self.rfile.read(size)))
                print(f"[config-ui] max loss saved enabled={risk['enabled']} amount={risk['amount']} emails={len(risk['emails'])}", flush=True)
                self._send_json(200, runtime_payload(runtime_request({"action": "status"}) or offline_status()))
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(503, {"error": str(exc)})
            return
        if path != "/api/config":
            self._send_json(404, {"error": "Not found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_BODY_BYTES:
                raise ValueError("Configuration request size is invalid")
            value = json.loads(self.rfile.read(content_length))
            saved = save_configuration(value)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(200, {"saved": True, **saved, "revision": config_revision(saved)})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[config-ui] {self.address_string()} {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAlgo strategy configuration and thread controls")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ConfigHandler)
    threading.Thread(target=market_refresher, name="market-cache", daemon=True).start()
    print(f"Configuration page ready at http://127.0.0.1:{args.port}")
    print("Runtime controls connect only to the scheduled SRSI supervisor.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping configuration page.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
