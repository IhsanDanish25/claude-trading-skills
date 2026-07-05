#!/usr/bin/env python3
"""Build the standard backtest bar cache from yfinance.

Fetches daily OHLCV for the 'power' universe in backtest_harness/standard_universe.json
(S&P 500 + the live SP80_UNIVERSE subset + benchmarks) and writes each symbol as a
gzipped {SYM}.json.gz in backtest_harness/cache/, in the same {"bars": [...]} shape
load_cached() reads.

Gzipped + committed (git add -f) so the harness runs in the team's no-network CI
(historical bars don't change). Re-run only to extend the window or add symbols.

Usage:
    python3 backtest_harness/build_standard_cache.py [--years 5] [--force] [--only live]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest_harness import data  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("build_standard_cache")

MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "standard_universe.json")


def _load_universe(only: str) -> list[str]:
    with open(MANIFEST) as f:
        m = json.load(f)
    if only == "live":
        return sorted(set(m["live"]) | set(m["benchmarks"]))
    return m["power"]


def _to_bars(hist) -> list[dict]:
    rows = []
    for ts, row in hist.iterrows():
        rows.append({
            "date":   ts.date().isoformat(),
            "open":   round(float(row["Open"]),   4),
            "high":   round(float(row["High"]),   4),
            "low":    round(float(row["Low"]),    4),
            "close":  round(float(row["Close"]),  4),
            "volume": float(row["Volume"]),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--force", action="store_true", help="re-fetch symbols already cached")
    ap.add_argument("--only", choices=["live", "power"], default="power")
    ap.add_argument("--sleep", type=float, default=0.25)
    args = ap.parse_args()

    import yfinance as yf

    symbols = _load_universe(args.only)
    period = f"{max(1, int(math.ceil(args.years))) + 1}y"
    log.info("Standard cache build: %d symbols | period=%s | force=%s | dir=%s",
             len(symbols), period, args.force, data.CACHE_DIR)

    todo = [s for s in symbols
            if args.force or not os.path.exists(data._cache_path_gz(s))]
    log.info("%d already cached (gz), fetching %d", len(symbols) - len(todo), len(todo))

    n_ok, n_fail, n_empty = 0, 0, 0
    for i, sym in enumerate(todo):
        try:
            hist = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=True)
            if hist is None or hist.empty:
                n_empty += 1
                log.warning("empty: %s", sym)
            else:
                bars = _to_bars(hist)
                if bars:
                    data.save_cached_gz(sym, bars)
                    n_ok += 1
                else:
                    n_empty += 1
        except Exception as e:  # noqa: BLE001
            n_fail += 1
            log.warning("%s failed: %s", sym, str(e)[:120])
        if args.sleep and i < len(todo) - 1:
            time.sleep(args.sleep)
        if (i + 1) % 50 == 0:
            log.info("… %d/%d (ok=%d empty=%d fail=%d)", i + 1, len(todo), n_ok, n_empty, n_fail)

    total = len(data.list_cached_symbols())
    log.info("Done: fetched=%d empty=%d fail=%d | total cached symbols now=%d",
             n_ok, n_empty, n_fail, total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
