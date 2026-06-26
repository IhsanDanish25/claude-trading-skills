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

Signal handling: SIGTERM/SIGINT break out of the sleep loop cleanly so
Railway's graceful-shutdown window (default 10s) doesn't end in SIGKILL.
Memory is logged each tick so OOM trends are visible before the kill.
"""
from __future__ import annotations

import logging
import os
import resource
import signal
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
SHUTDOWN_GRACE_SECONDS = 8  # must beat Railway's 10s SIGKILL window

# Set by SIGTERM/SIGINT handler. Checked at the top of every sleep + tick.
_shutdown_requested = False


def _request_shutdown(signum, frame):
    global _shutdown_requested
    if not _shutdown_requested:
        log.info("Signal %s received — shutting down after current tick", signum)
    _shutdown_requested = True


def _rss_mb() -> float:
    # ru_maxrss is KB on Linux, bytes on macOS. The platform switch makes
    # the math identical in both cases once normalized to MB.
    import sys as _sys
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 if _sys.platform == "darwin" else 1.0  # bytes vs KB
    return rss / divisor / 1024.0  # → MB


def _interrupted_sleep(seconds: float) -> None:
    """Sleep that wakes on SIGTERM/SIGINT instead of running to completion."""
    global _shutdown_requested
    deadline = time.monotonic() + seconds
    while not _shutdown_requested:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        # sleep in 1s slices so signal handlers get a chance to flip the flag
        time.sleep(min(1.0, remaining))


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
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    startup_health_check()
    log.info("Worker daemon started — scheduler fires every %ds "
             "(RSS=%.1f MB, pid=%d)", TICK_SECONDS, _rss_mb(), os.getpid())

    tick_count = 0
    while not _shutdown_requested:
        tick_count += 1
        log.info("Firing scheduler (tick #%d, RSS=%.1f MB)...",
                 tick_count, _rss_mb())
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

        # Periodic memory observation so OOM trends are visible before kill.
        if tick_count % 5 == 0:
            log.info("Memory check (tick #%d): RSS=%.1f MB",
                     tick_count, _rss_mb())

        log.info("Sleeping %ds until next tick...", TICK_SECONDS)
        _interrupted_sleep(TICK_SECONDS)

    log.info("Worker exiting cleanly (RSS=%.1f MB, ticks completed=%d)",
             _rss_mb(), tick_count)
    sys.exit(0)


if __name__ == "__main__":
    main()
