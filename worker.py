#!/usr/bin/env python3
"""
Railway worker daemon.

Fires scheduler.py every 10 minutes. scheduler.py checks the current ET time
and dispatches the right routine (pre_market / market_open / midday /
market_close / weekly_review), or exits quietly when nothing is scheduled.
This process runs forever so Railway keeps it alive between ticks.

On startup: runs a health check that tests Alpaca + FMP connectivity so
credential problems surface immediately in the logs (not hours later when
a routine finally fires).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | worker | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("worker")

TICK_SECONDS = 600  # 10 minutes


def startup_health_check() -> None:
    """Test Alpaca + FMP connectivity on boot. Logs results, never crashes."""
    log.info("=" * 50)
    log.info("STARTUP HEALTH CHECK")
    log.info("=" * 50)

    # Check env vars
    required = {
        "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
        "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        "FMP_API_KEY": os.environ.get("FMP_API_KEY", ""),
    }
    for name, val in required.items():
        if val:
            log.info("  %s: SET (%s...%s)", name, val[:4], val[-4:])
        else:
            log.error("  %s: MISSING", name)

    missing = [k for k, v in required.items() if not v]
    if missing:
        log.error("HEALTH CHECK FAILED: missing env vars: %s", ", ".join(missing))
        log.error("Set them in Railway → worker → Variables tab")
        return

    # Test Alpaca connection
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
        from alpaca_client import AlpacaClient
        client = AlpacaClient()
        acct = client.get_account()
        equity = float(acct.get("equity", 0))
        cash = float(acct.get("cash", 0))
        status = acct.get("status", "unknown")
        log.info("  ALPACA: CONNECTED ✓ (status=%s, equity=$%.2f, cash=$%.2f)",
                 status, equity, cash)
    except Exception as e:
        log.error("  ALPACA: FAILED ✗ — %s", e)
        log.error("  Fix: regenerate API keys at app.alpaca.markets → API Keys")
        log.error("  Then update ALPACA_API_KEY + ALPACA_SECRET_KEY in Railway Variables")
        return

    # Test FMP connection
    try:
        import requests
        fmp_key = os.environ.get("FMP_API_KEY", "")
        r = requests.get(
            "https://financialmodelingprep.com/stable/quote",
            params={"symbol": "AAPL", "apikey": fmp_key},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            price = data[0].get("price", 0) if isinstance(data, list) else 0
            log.info("  FMP: CONNECTED ✓ (AAPL=$%.2f)", price)
        else:
            log.warning("  FMP: empty response (may be rate-limited)")
    except Exception as e:
        log.warning("  FMP: FAILED — %s (non-fatal, screener may degrade)", e)

    log.info("HEALTH CHECK COMPLETE")
    log.info("=" * 50)


def main() -> None:
    startup_health_check()
    log.info("Worker daemon started — scheduler fires every %ds", TICK_SECONDS)
    while True:
        log.info("Firing scheduler...")
        try:
            result = subprocess.run(
                [sys.executable, "scheduler.py"],
                timeout=540,  # 9-min cap; leaves 1 min before next tick
            )
            if result.returncode not in (0, None):
                log.warning("Scheduler exited with code %d", result.returncode)
        except subprocess.TimeoutExpired:
            log.error("Scheduler timed out after 540s")
        except Exception as exc:
            log.error("Scheduler failed: %s", exc)
        log.info("Sleeping %ds until next tick...", TICK_SECONDS)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
