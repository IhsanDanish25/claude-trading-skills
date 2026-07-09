"""
Short interest fetcher — yfinance (free, no API key).

Data source: Yahoo Finance `.info` attribute.
Covers: ~95% of SP80 universe (NASDAQ publishes short interest in their
monthly table; yfinance surfaces this). Data is as-of the most recent
NASDAQ settlement date (typically the 15th or last business day of month).

Fields per symbol:
  - short_interest_pct  (float) — SI as % of float, e.g. 15.4
  - short_interest      (int)   — raw shares short
  - days_to_cover       (float) — computed locally: SI / 22-day avg volume
  - average_volume      (int)   — 22-day avg daily volume

FMP drain: 0 calls (yfinance only).
"""
from __future__ import annotations

import datetime
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yfinance as yf

from core.config import SP80_UNIVERSE

log = logging.getLogger(__name__)

# In-process cache — survives across screen() calls in the same run (SI is
# a fixed monthly snapshot anyway; redundant requests only waste time).
_cache: dict[str, dict] | None = None


def _fetch_one(sym: str) -> tuple[str, dict | None]:
    """
    Fetch short interest info for a single symbol via yfinance.

    Returns (symbol, data_dict_or_None).
    """
    try:
        ticker = yf.Ticker(sym)
        info = ticker.fast_info if hasattr(ticker, "fast_info") else ticker.info

        # Pull what we can — yfinance surfaces NASDAQ-reported SI as these fields
        si_pct     = info.get("shortPercentOfFloat")       # e.g. 0.154 = 15.4%
        si_shares  = info.get("sharesShort")               # raw share count
        avg_vol    = (
            info.get("averageVolume")
            or info.get("averageDailyVolume")
            or 0
        )

        if si_pct is None and si_shares is None:
            return sym, None

        # Compute days-to-cover locally: SI / 22-day avg daily volume
        dtc = 0.0
        if si_shares and avg_vol and float(avg_vol) > 0:
            dtc = float(si_shares) / float(avg_vol)

        # If si_pct is missing but we have raw shares, try to derive from float
        if si_pct is None and si_shares:
            float_shares = info.get("floatShares")
            if float_shares and float(float_shares) > 0:
                si_pct = float(si_shares) / float(float_shares)

        pct_val = float(si_pct) * 100 if si_pct is not None else 0.0

        return sym, {
            "short_interest_pct":  round(pct_val, 2),
            "short_interest":      int(si_shares or 0),
            "days_to_cover":       round(dtc, 1),
            "average_volume":      int(avg_vol or 0),
            "data_age_days":       None,   # yfinance doesn't expose report date
        }
    except Exception:
        return sym, None


def get_short_interest(symbols: list[str] | None = None) -> dict[str, dict]:
    """
    Fetch short interest for all SP80 stocks (or supplied list) via yfinance.

    Returns {symbol: {short_interest_pct, short_interest, days_to_cover, average_volume}}.

    In-process cached for the remainder of the caller's run.

    FMP drain: 0.
    """
    global _cache
    if _cache is not None:
        log.info("ShortInterest: using in-process cache (%d symbols)", len(_cache))
        return _cache

    target = symbols if symbols else list(SP80_UNIVERSE)
    log.info(f"ShortInterest: fetching via yfinance for {len(target)} symbols...")

    result: dict[str, dict] = {}
    failures: list[str] = []

    # Fetch in parallel — yfinance .info makes 1 HTTP request per symbol
    # but we can safely run ~8 threads without hitting rate limits.
    # Each fetch takes ~0.5-1.5s; total wall time: ~15-30s for 50 symbols.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_one, sym): sym for sym in target}
        for i, future in enumerate(as_completed(futures), 1):
            sym, data = future.result()
            if data is not None:
                result[sym] = data
            else:
                failures.append(sym)
            # Progress log at halfway
            if i == len(target) // 2:
                log.info(f"  ShortInterest: {i}/{len(target)} fetched...")

    log.info(f"  ShortInterest: {len(result)}/{len(target)} symbols with data "
             f"({len(failures)} not found)")
    if failures:
        log.debug(f"  Not found: {failures}")

    _cache = result
    return result