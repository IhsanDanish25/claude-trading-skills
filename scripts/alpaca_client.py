#!/usr/bin/env python3
"""Centralized Alpaca API client with multi-environment credential loading.

Credential resolution order:
1. Explicit constructor arguments
2. Standard env vars: ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
3. Railway service variables (same names — Railway injects them natively)

Railway detection: when RAILWAY_ENVIRONMENT or RAILWAY_SERVICE_NAME is set,
error messages include Railway-specific guidance (dashboard link, variable
names to check).

Usage::

    from alpaca_client import AlpacaClient

    client = AlpacaClient()          # loads from env
    if not client.is_configured():
        print(client.setup_hint())   # environment-aware help
        sys.exit(1)

    info = client.verify_connection()
    positions = client.get_positions()
"""

from __future__ import annotations

import os
import sys
from typing import Any

try:
    import requests
except ImportError as e:
    raise RuntimeError("alpaca_client requires the `requests` package") from e

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"

_ENV_KEY = "ALPACA_API_KEY"
_ENV_SECRET = "ALPACA_SECRET_KEY"
_ENV_PAPER = "ALPACA_PAPER"


def is_railway() -> bool:
    return bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_SERVICE_NAME")
        or os.environ.get("RAILWAY_PROJECT_NAME")
    )


class AlpacaClient:
    """Lightweight Alpaca REST client (no SDK dependency)."""

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        paper: bool | None = None,
        timeout: float = 10.0,
    ) -> None:
        # .strip() guards against a trailing newline or stray space on a
        # pasted Railway variable, which becomes part of the key and causes
        # a 401 that looks like a bad/expired credential.
        self.api_key = (api_key or os.environ.get(_ENV_KEY) or "").strip() or None
        self.secret_key = (secret_key or os.environ.get(_ENV_SECRET) or "").strip() or None

        if paper is not None:
            self.paper = paper
        else:
            self.paper = os.environ.get(_ENV_PAPER, "true").lower() == "true"

        self.timeout = timeout

    # ── helpers ──────────────────────────────────────────────

    def is_configured(self) -> bool:
        return bool(self.api_key and self.secret_key)

    @property
    def trading_url(self) -> str:
        return PAPER_BASE_URL if self.paper else LIVE_BASE_URL

    @property
    def data_url(self) -> str:
        return DATA_BASE_URL

    @property
    def mode_label(self) -> str:
        return "paper" if self.paper else "live"

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key or "",
            "APCA-API-SECRET-KEY": self.secret_key or "",
        }

    def setup_hint(self) -> str:
        """Return environment-aware setup instructions."""
        if is_railway():
            return (
                "Alpaca credentials not found in Railway environment.\n"
                "Set these variables in your Railway service dashboard:\n"
                f"  {_ENV_KEY}     = <your API key ID>\n"
                f"  {_ENV_SECRET}  = <your secret key>\n"
                f"  {_ENV_PAPER}   = true   (or false for live)\n"
                "\n"
                "Railway dashboard → your service → Variables tab."
            )
        return (
            "Alpaca credentials not found.\n"
            "Set environment variables:\n"
            f"  export {_ENV_KEY}='your_api_key_id'\n"
            f"  export {_ENV_SECRET}='your_secret_key'\n"
            f"  export {_ENV_PAPER}='true'\n"
        )

    # ── API methods ─────────────────────────────────────────

    def get_account(self) -> dict[str, Any]:
        resp = requests.get(
            f"{self.trading_url}/v2/account",
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_positions(self) -> list[dict[str, Any]]:
        resp = requests.get(
            f"{self.trading_url}/v2/positions",
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_orders(self, status: str = "all", limit: int = 50) -> list[dict[str, Any]]:
        resp = requests.get(
            f"{self.trading_url}/v2/orders",
            headers=self._headers(),
            params={"status": status, "limit": limit},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_portfolio_history(self, period: str = "1M", timeframe: str = "1D") -> dict[str, Any]:
        resp = requests.get(
            f"{self.trading_url}/v2/account/portfolio/history",
            headers=self._headers(),
            params={"period": period, "timeframe": timeframe},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_latest_quote(self, symbol: str) -> dict[str, Any]:
        resp = requests.get(
            f"{self.data_url}/v2/stocks/{symbol}/quotes/latest",
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_asset(self, symbol: str) -> dict[str, Any] | None:
        resp = requests.get(
            f"{self.trading_url}/v2/assets/{symbol}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # ── connection verification ──────────────────────────────

    def verify_connection(self) -> dict[str, Any]:
        """Test credentials and return account info.

        Raises ``requests.HTTPError`` on auth failure, ``RuntimeError``
        when credentials are missing.
        """
        if not self.is_configured():
            raise RuntimeError(self.setup_hint())
        return self.get_account()


# ── CLI entry point ──────────────────────────────────────────


def _print_account(account: dict[str, Any]) -> None:
    print(f"  Status:          {account.get('status')}")
    print(f"  Account Number:  {account.get('account_number')}")
    print(f"  Equity:          ${float(account.get('equity', 0)):,.2f}")
    print(f"  Cash:            ${float(account.get('cash', 0)):,.2f}")
    print(f"  Buying Power:    ${float(account.get('buying_power', 0)):,.2f}")
    print(f"  Portfolio Value:  ${float(account.get('portfolio_value', 0)):,.2f}")

    if account.get("account_blocked") or account.get("trading_blocked"):
        print("\n  WARNING: Account has restrictions")
        if account.get("account_blocked"):
            print("    - Account is blocked")
        if account.get("trading_blocked"):
            print("    - Trading is blocked")


def _print_positions(positions: list[dict[str, Any]]) -> None:
    if not positions:
        print("  (no open positions)")
        return

    total_value = 0.0
    total_pl = 0.0
    for pos in positions:
        sym = pos.get("symbol")
        qty = float(pos.get("qty", 0))
        avg = float(pos.get("avg_entry_price", 0))
        cur = float(pos.get("current_price", 0))
        mv = float(pos.get("market_value", 0))
        upl = float(pos.get("unrealized_pl", 0))
        uplp = float(pos.get("unrealized_plpc", 0)) * 100
        total_value += mv
        total_pl += upl
        sign = "+" if upl >= 0 else ""
        print(
            f"  {sym}: {qty:.2f} shares @ ${avg:.2f} → ${cur:.2f}  "
            f"({sign}${upl:,.2f} / {sign}{uplp:.1f}%)"
        )

    sign = "+" if total_pl >= 0 else ""
    print(f"\n  Total Value: ${total_value:,.2f}  Unrealized P/L: {sign}${total_pl:,.2f}")


def main() -> int:
    client = AlpacaClient()

    if not client.is_configured():
        print("FAIL: " + client.setup_hint())
        return 1

    key_preview = f"{client.api_key[:6]}...{client.api_key[-4:]}"
    env_label = "Railway" if is_railway() else "local"
    print(f"Alpaca Connection Check  ({env_label}, {client.mode_label})")
    print(f"Key: {key_preview}")
    print("=" * 50)

    # 1. Account
    print("\n[1/3] Account")
    try:
        account = client.verify_connection()
        print("  OK")
        _print_account(account)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        print(f"  FAIL (HTTP {code})")
        if code == 401:
            print("  Check that your API key and secret are correct.")
            if is_railway():
                print("  Railway dashboard → service → Variables tab.")
        elif code == 403:
            print("  Regenerate keys with full permissions in Alpaca dashboard.")
        return 1
    except requests.ConnectionError:
        print("  FAIL (network error — check connectivity)")
        return 1

    # 2. Positions
    print("\n[2/3] Positions")
    try:
        positions = client.get_positions()
        print("  OK")
        _print_positions(positions)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        print(f"  FAIL (HTTP {code})")

    # 3. Market data (non-fatal)
    print("\n[3/3] Market Data")
    try:
        quote = client.get_latest_quote("AAPL")
        q = quote.get("quote", {})
        print(f"  OK — AAPL bid ${q.get('bp', 0):.2f} / ask ${q.get('ap', 0):.2f}")
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        if code == 402:
            print("  SKIP (paid subscription required — normal for free tier)")
        else:
            print(f"  WARN (HTTP {code}) — won't affect portfolio analysis")
    except requests.ConnectionError:
        print("  WARN (network) — won't affect portfolio analysis")

    print("\n" + "=" * 50)
    print("Connection verified. Portfolio Manager skill is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
