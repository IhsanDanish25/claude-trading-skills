#!/usr/bin/env python3
"""Routine dispatcher for Railway cron services.

Each Railway service sets ``ROUTINE`` env var and a cron schedule.
This script:
1. Runs the Alpaca auto-connect chain (validates creds, writes state)
2. Dispatches to the matching routine module

Usage (Railway start command):
    python3 routines/run.py

Required env var:
    ROUTINE  — one of: pre_market, market_open, midday_review, market_close, weekly_review
"""

import importlib
import logging
import os
import sys
from pathlib import Path

# Make scripts/ importable for auto-connect chain
_scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from alpaca_auto_connect import run_chain  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

VALID_ROUTINES = {
    "pre_market",
    "market_open",
    "midday_review",
    "market_close",
    "weekly_review",
}

NEEDS_ALPACA = {"pre_market", "market_open", "midday_review", "market_close"}


def main() -> int:
    routine = os.environ.get("ROUTINE", "").strip()
    if not routine:
        logger.error("ROUTINE env var not set. Expected one of: %s",
                      ", ".join(sorted(VALID_ROUTINES)))
        return 1
    if routine not in VALID_ROUTINES:
        logger.error("Unknown routine '%s'. Expected one of: %s",
                      routine, ", ".join(sorted(VALID_ROUTINES)))
        return 1

    # Auto-connect chain for routines that touch Alpaca
    if routine in NEEDS_ALPACA:
        rc = run_chain(dry_run=False, json_output=False)
        if rc != 0:
            logger.error("Alpaca auto-connect failed (exit %d). Aborting %s.", rc, routine)
            return rc

    # Import and run the routine
    logger.info("Dispatching routine: %s", routine)
    try:
        mod = importlib.import_module(routine)
        mod.run()
    except Exception:
        logger.exception("Routine %s failed", routine)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
