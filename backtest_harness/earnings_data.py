"""Historical earnings-surprise cache for the backtest harness.

Uses yfinance (free, no API key) to pull EPS surprise history per-symbol.
Each symbol's full earnings history is cached on disk as a JSON file and never
re-fetched once written (historical earnings don't change).

Cache layout: backtest_harness/cache/earnings/earnings_{SYMBOL}.json

Replaces the original FMP-based implementation (blocked on free-tier plan).
"""
from __future__ import annotations

import datetime
import json
import logging
import os

from core.earnings_screener import fetch_symbol_earnings

log = logging.getLogger("backtest.earnings_data")

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
EARNINGS_CACHE_DIR = os.path.join(CACHE_DIR, "earnings")
os.makedirs(EARNINGS_CACHE_DIR, exist_ok=True)


def _as_date(d) -> datetime.date:
    return d if isinstance(d, datetime.date) else datetime.date.fromisoformat(d)


def get_symbol_earnings(symbol: str) -> list[dict]:
    """Earnings history for one symbol, disk-cached forever.

    Cache key: backtest_harness/cache/earnings/earnings_{symbol}.json
    Returns [{date, eps_estimate, reported_eps, surprise_pct}].
    """
    path = os.path.join(EARNINGS_CACHE_DIR, f"earnings_{symbol}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (ValueError, OSError):
            pass  # corrupt cache — re-fetch
    rows = fetch_symbol_earnings(symbol, sleep=True)
    if rows is None:
        return []  # fetch error — skip caching so next run retries
    try:
        with open(path, "w") as f:
            json.dump(rows, f)
    except OSError as e:
        log.warning("Could not cache earnings for %s: %s", symbol, e)
    return rows


def get_historical_surprises(
    symbols: list[str],
    start_date,
    end_date,
    min_surprise_pct: float = 0.0,
) -> list[dict]:
    """All EPS surprises >= min_surprise_pct within [start_date, end_date] for symbols.

    Each row: {symbol, date, surprise_pct, actual, estimated}.
    Caches per-symbol on disk via get_symbol_earnings(); never re-fetches a cached symbol.
    Sorted by (date, -surprise_pct).

    On first run, expect ~1s per uncached symbol (yfinance sleep). Subsequent
    runs return immediately from disk cache.
    """
    start, end = _as_date(start_date), _as_date(end_date)
    start_iso, end_iso = start.isoformat(), end.isoformat()
    out: list[dict] = []

    for i, sym in enumerate(symbols):
        rows = get_symbol_earnings(sym)
        for r in rows:
            d = r.get("date", "")
            sp = r.get("surprise_pct")
            if not d or sp is None:
                continue
            if not (start_iso <= d <= end_iso):
                continue
            if sp < min_surprise_pct:
                continue
            out.append({
                "symbol": sym,
                "date": d,
                "surprise_pct": round(float(sp), 4),
                "actual": r.get("reported_eps"),
                "estimated": r.get("eps_estimate"),
            })
        if (i + 1) % 50 == 0:
            log.info("Earnings cache: processed %d/%d symbols", i + 1, len(symbols))

    out.sort(key=lambda r: (r["date"], -(r["surprise_pct"] or 0)))
    log.info("Historical surprises: %d rows from %d symbols (%s..%s)",
             len(out), len(symbols), start, end)
    return out
