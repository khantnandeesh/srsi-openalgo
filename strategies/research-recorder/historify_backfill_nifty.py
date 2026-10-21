"""Backfill recent NIFTY spot and near-ATM option bars into OpenAlgo Historify.

Run this on the OpenAlgo VM, where the broker API key is read from the existing
strategy config .env file. This script only downloads market history; it has no
order methods or strategy controls.
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openalgo import api
from services.historify_service import download_data


ROOT = Path(__file__).resolve().parent
ENV_PATHS = (ROOT.parent / "config-ui" / ".env", ROOT.parents[1] / ".env")
IST = ZoneInfo("Asia/Kolkata")


def load_api_key() -> str:
    for path in ENV_PATHS:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    key = os.getenv("OPENALGO_API_KEY")
    if not key:
        raise RuntimeError("OPENALGO_API_KEY is missing from the VM environment files")
    return key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2026-09-01", help="YYYY-MM-DD")
    parser.add_argument("--end-date", default=datetime.now(IST).date().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--strike-count", type=int, default=5, help="Strikes above and below ATM")
    parser.add_argument("--expiry-count", type=int, default=2, help="Nearest active expiries")
    args = parser.parse_args()
    if not 1 <= args.strike_count <= 20:
        parser.error("--strike-count must be between 1 and 20")
    if not 1 <= args.expiry_count <= 4:
        parser.error("--expiry-count must be between 1 and 4")
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    if end < start:
        parser.error("--end-date must be on or after --start-date")

    api_key = load_api_key()
    client = api(api_key=api_key, host="http://127.0.0.1:5000")
    expiries_response = client.expiry(
        symbol="NIFTY",
        exchange="NFO",
        instrumenttype="options",
    )
    if not isinstance(expiries_response, dict) or expiries_response.get("status") != "success":
        raise RuntimeError(f"NIFTY expiry lookup failed: {expiries_response}")

    eligible: list[tuple[date, str]] = []
    for raw in expiries_response.get("data", []):
        try:
            expiry = datetime.strptime(str(raw), "%d-%b-%y").date()
        except ValueError:
            continue
        if expiry >= datetime.now(IST).date():
            eligible.append((expiry, expiry.strftime("%d%b%y").upper()))
    eligible.sort()
    chosen = eligible[: args.expiry_count]
    if not chosen:
        raise RuntimeError("No current or future NIFTY option expiries are available")

    symbols: list[tuple[str, str]] = [("NIFTY", "NSE_INDEX")]
    for expiry, compact_expiry in chosen:
        chain = client.optionchain(
            underlying="NIFTY",
            exchange="NSE_INDEX",
            expiry_date=compact_expiry,
            strike_count=args.strike_count,
        )
        if not isinstance(chain, dict) or chain.get("status") != "success":
            print(f"CHAIN_SKIP expiry={compact_expiry} reason={str(chain)[:200]}", flush=True)
            continue
        for strike in chain.get("chain", []):
            for leg_key in ("ce", "pe"):
                leg = strike.get(leg_key) if isinstance(strike, dict) else None
                if isinstance(leg, dict) and leg.get("symbol"):
                    symbols.append((str(leg["symbol"]), "NFO"))
        print(
            f"CHAIN expiry={compact_expiry} strikes={len(chain.get('chain', []))} "
            f"atm={chain.get('atm_strike')}",
            flush=True,
        )

    symbols = list(dict.fromkeys(symbols))
    print(
        f"BACKFILL start={start.isoformat()} end={end.isoformat()} "
        f"interval=1m symbols={len(symbols)}; using Historify broker-history downloader",
        flush=True,
    )

    failures = 0
    total_records = 0
    for index, (symbol, exchange) in enumerate(symbols, start=1):
        try:
            ok, result, status_code = download_data(
                symbol=symbol,
                exchange=exchange,
                interval="1m",
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                api_key=api_key,
            )
            count = int(result.get("records", 0) or 0)
            total_records += count
            if ok:
                print(f"{index}/{len(symbols)} OK {symbol}:{exchange} rows={count}", flush=True)
            else:
                failures += 1
                print(f"{index}/{len(symbols)} FAIL {symbol}:{exchange} status={status_code} message={str(result.get('message', ''))[:180]}", flush=True)
        except Exception as exc:
            failures += 1
            print(f"{index}/{len(symbols)} FAIL {symbol}:{exchange} {type(exc).__name__}: {str(exc)[:180]}", flush=True)
        if index < len(symbols):
            time.sleep(0.8)

    try:
        client.close()
    except Exception:
        pass
    print(f"DONE symbols={len(symbols)} records={total_records} failures={failures}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
