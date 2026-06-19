#!/usr/bin/env python3
"""
Railway worker daemon.

Fires scheduler.py every 10 minutes. scheduler.py checks the current ET time
and dispatches the right routine (pre_market / market_open / midday /
market_close / weekly_review), or exits quietly when nothing is scheduled.
This process runs forever so Railway keeps it alive between ticks.
"""
from __future__ import annotations

import logging
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


def main() -> None:
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
