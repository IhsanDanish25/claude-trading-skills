"""
Breakout screener — price above 50-day resistance with 1.5x volume confirmation.

Uses FMP /stable/historical-price-eod for daily OHLCV.
"""
from __future__ import annotations

import logging

from core.fmp import _get, _STABLE

log = logging.getLogger(__name__)

_RESISTANCE_PERIOD = 50
_VOLUME_MULT = 1.5
_AVG_VOLUME_PERIOD = 20
_MIN_PRICE = 5.0


def _get_breakout_universe() -> list[str]:
    """Top liquid S&P names for breakout scanning."""
    return [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
        "JPM", "LLY", "UNH", "V", "XOM", "COST", "MA", "HD", "WMT", "NFLX",
        "PG", "JNJ", "ORCL", "ABBV", "CRM", "BAC", "AMD", "MRK", "CVX",
        "KO", "PEP", "ADBE", "TMO", "LIN", "ACN", "MCD", "CSCO", "WFC",
        "ABT", "GE", "DHR", "TXN", "IBM", "INTU", "AMGN", "QCOM", "NEE",
        "RTX", "PM", "VZ", "LOW", "UBER", "ISRG", "SPGI", "GS", "BKNG",
        "MS", "ELV", "COP", "CAT", "SYK", "MDT", "T", "DE", "BLK", "PFE",
        "ADP", "SCHW", "C", "GILD", "AMAT", "SBUX", "ADI", "MDLZ", "MMC",
        "VRTX", "LRCX", "AMT", "CI", "MU", "AXP", "PLD", "SO", "ETN",
        "ZTS", "CB", "NOW", "REGN", "TJX", "BSX", "DUK", "EOG", "PH",
    ]


def screen_breakout(volume_mult: float = _VOLUME_MULT) -> list[dict]:
    """Screen for stocks breaking above 50-day resistance on high volume.

    Criteria:
    - Today's close > max(high) of prior 50 days (resistance breakout)
    - Today's volume >= 1.5x 20-day average volume
    - Price above $5

    Returns candidates sorted by volume ratio descending.
    """
    symbols = _get_breakout_universe()
    candidates = []

    for sym in symbols:
        try:
            bars = _get(f"{_STABLE}/historical-price-eod/full", {"symbol": sym})
            if not bars or not isinstance(bars, list):
                continue
            if len(bars) < _RESISTANCE_PERIOD + 1:
                continue

            today = bars[0]
            price = today.get("close", 0)
            volume = today.get("volume", 0)

            if price < _MIN_PRICE:
                continue

            prior_bars = bars[1:_RESISTANCE_PERIOD + 1]
            resistance = max(b.get("high", 0) for b in prior_bars)

            if price <= resistance:
                continue

            vol_bars = bars[1:_AVG_VOLUME_PERIOD + 1]
            volumes = [b.get("volume", 0) for b in vol_bars if b.get("volume")]
            if not volumes:
                continue
            avg_volume = sum(volumes) / len(volumes)
            if avg_volume <= 0:
                continue

            vol_ratio = volume / avg_volume
            if vol_ratio < volume_mult:
                continue

            breakout_pct = round((price - resistance) / resistance * 100, 2)

            candidates.append({
                "symbol": sym,
                "price": round(price, 2),
                "resistance": round(resistance, 2),
                "breakout_pct": breakout_pct,
                "volume": int(volume),
                "avg_volume": int(avg_volume),
                "volume_ratio": round(vol_ratio, 2),
                "date": today.get("date", ""),
                "strategy": "breakout",
            })
        except Exception as e:
            log.warning("breakout %s skip: %s", sym, e)
            continue

    candidates.sort(key=lambda x: x["volume_ratio"], reverse=True)
    log.info("Breakout screen: %d candidates from %d symbols", len(candidates), len(symbols))
    return candidates
