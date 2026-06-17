#!/usr/bin/env python3
"""Alpaca auto-connection chain.

Runs on startup (Railway deploy, cron, or manual) to:
1. Detect environment (Railway / local / CI)
2. Load credentials from env vars
3. Verify connection to Alpaca paper or live API
4. Pull account snapshot (equity, positions, orders)
5. Write connection state to ``state/alpaca_connection.json``

All downstream skills can import ``load_connection_state()`` to check
whether Alpaca is available without making another API call.

Exit codes:
    0  — connected, state file written
    1  — credentials missing or auth failed
    2  — network error (transient, retry later)

Usage::

    # Railway: add as a startup command or health-check
    python3 scripts/alpaca_auto_connect.py

    # Local: run once after setting env vars
    python3 scripts/alpaca_auto_connect.py

    # Dry-run: validate config without writing state
    python3 scripts/alpaca_auto_connect.py --dry-run

    # JSON output (for piping into other tools)
    python3 scripts/alpaca_auto_connect.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpaca_client import AlpacaClient, is_railway  # noqa: E402

try:
    import requests
except ImportError:
    print("ERROR: requests library required — pip install requests")
    sys.exit(1)

STATE_DIR = Path(__file__).resolve().parents[1] / "state"
STATE_FILE = STATE_DIR / "alpaca_connection.json"


def _snapshot(client: AlpacaClient) -> dict[str, Any]:
    """Pull account + positions + recent orders into a single dict."""
    account = client.get_account()

    positions_raw = client.get_positions()
    positions = []
    for p in positions_raw:
        positions.append({
            "symbol": p.get("symbol"),
            "qty": float(p.get("qty", 0)),
            "avg_entry_price": float(p.get("avg_entry_price", 0)),
            "current_price": float(p.get("current_price", 0)),
            "market_value": float(p.get("market_value", 0)),
            "unrealized_pl": float(p.get("unrealized_pl", 0)),
            "unrealized_plpc": float(p.get("unrealized_plpc", 0)),
        })

    orders_raw = client.get_orders(status="open", limit=20)
    open_orders = []
    for o in orders_raw:
        open_orders.append({
            "id": o.get("id"),
            "symbol": o.get("symbol"),
            "side": o.get("side"),
            "type": o.get("type"),
            "qty": o.get("qty"),
            "status": o.get("status"),
            "created_at": o.get("created_at"),
        })

    return {
        "connected": True,
        "mode": client.mode_label,
        "environment": "railway" if is_railway() else "local",
        "connected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": {
            "status": account.get("status"),
            "account_number": account.get("account_number"),
            "equity": float(account.get("equity", 0)),
            "cash": float(account.get("cash", 0)),
            "buying_power": float(account.get("buying_power", 0)),
            "portfolio_value": float(account.get("portfolio_value", 0)),
            "account_blocked": account.get("account_blocked", False),
            "trading_blocked": account.get("trading_blocked", False),
        },
        "positions_count": len(positions),
        "positions": positions,
        "open_orders_count": len(open_orders),
        "open_orders": open_orders,
    }


def _failure_state(reason: str, environment: str) -> dict[str, Any]:
    return {
        "connected": False,
        "mode": None,
        "environment": environment,
        "connected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": reason,
        "account": None,
        "positions_count": 0,
        "positions": [],
        "open_orders_count": 0,
        "open_orders": [],
    }


def write_state(state: dict[str, Any]) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    return STATE_FILE


def load_connection_state() -> dict[str, Any] | None:
    """Read the last connection state (for use by other skills)."""
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def run_chain(dry_run: bool = False, json_output: bool = False) -> int:
    env = "railway" if is_railway() else "local"
    client = AlpacaClient()

    # Step 1: Check credentials
    if not client.is_configured():
        reason = "credentials_missing"
        if not json_output:
            print(f"FAIL: {client.setup_hint()}")
        if not dry_run:
            write_state(_failure_state(reason, env))
        if json_output:
            print(json.dumps(_failure_state(reason, env), indent=2))
        return 1

    key_preview = f"{client.api_key[:6]}...{client.api_key[-4:]}"
    if not json_output:
        print(f"[chain] env={env}  mode={client.mode_label}  key={key_preview}")

    # Step 2: Verify connection
    try:
        snapshot = _snapshot(client)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        reason = f"auth_failed_{code}" if code in (401, 403) else f"http_{code}"
        if not json_output:
            print(f"FAIL: HTTP {code}")
            if code == 401:
                print("  Bad credentials. Check ALPACA_API_KEY / ALPACA_SECRET_KEY.")
            elif code == 403:
                print("  Forbidden. Regenerate keys with full permissions.")
        if not dry_run:
            write_state(_failure_state(reason, env))
        if json_output:
            print(json.dumps(_failure_state(reason, env), indent=2))
        return 1
    except requests.ConnectionError:
        reason = "network_error"
        if not json_output:
            print("FAIL: Network error — check connectivity.")
        if not dry_run:
            write_state(_failure_state(reason, env))
        if json_output:
            print(json.dumps(_failure_state(reason, env), indent=2))
        return 2

    # Step 3: Write state
    if not dry_run:
        path = write_state(snapshot)
        if not json_output:
            print(f"[chain] state written → {path}")

    # Step 4: Report
    acct = snapshot["account"]
    if json_output:
        print(json.dumps(snapshot, indent=2))
    else:
        print(f"[chain] CONNECTED")
        print(f"  Account:   {acct['account_number']}  ({acct['status']})")
        print(f"  Equity:    ${acct['equity']:,.2f}")
        print(f"  Cash:      ${acct['cash']:,.2f}")
        print(f"  Positions: {snapshot['positions_count']}")
        print(f"  Open orders: {snapshot['open_orders_count']}")
        if acct.get("account_blocked") or acct.get("trading_blocked"):
            print("  WARNING: account/trading blocked")
        for pos in snapshot["positions"]:
            sign = "+" if pos["unrealized_pl"] >= 0 else ""
            print(f"    {pos['symbol']}: {pos['qty']:.2f} sh "
                  f"@ ${pos['avg_entry_price']:.2f} → ${pos['current_price']:.2f} "
                  f"({sign}${pos['unrealized_pl']:,.2f})")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Alpaca auto-connection chain")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate without writing state file")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output JSON (for piping into other tools)")
    args = parser.parse_args()
    return run_chain(dry_run=args.dry_run, json_output=args.json_output)


if __name__ == "__main__":
    sys.exit(main())
