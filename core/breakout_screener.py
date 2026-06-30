"""
Breakout screener — price above 50-day resistance with 1.5x volume confirmation.

Uses FMP /stable/historical-price-eod for daily OHLCV.
"""
from __future__ import annotations

import logging
import statistics

from core.fmp import get_daily_bars, get_screener_universe

log = logging.getLogger(__name__)


def screen_breakout(
    symbols: list[str] | None = None,
    lookback: int = 50,
    volume_mult: float = 1.5,
    min_price: float = 10.0,
    max_symbols: int = 200,
) -> list[dict]:
    """Find stocks breaking above their 50-day high on elevated volume."""
    if symbols is None:
        symbols = get_screener_universe(limit=max_symbols)
        if not symbols:
            log.warning("Breakout screen: empty universe from screener")
            return []

    log.info("Breakout screen: %d symbols, %d-day resistance, %.1fx vol",
             len(symbols), lookback, volume_mult)

    candidates = []
    for sym in symbols:
        try:
            bars = get_daily_bars(sym, days=lookback + 10)
            if len(bars) < lookback:
                continue

            price = bars[0]["close"]
            today_vol = bars[0]["volume"]
            if price < min_price:
                continue

            prior_highs = [b["high"] for b in bars[1:lookback + 1]]
            resistance = max(prior_highs)

            if price <= resistance:
                continue

            avg_vol = statistics.mean(
                [b["volume"] for b in bars[1:lookback + 1] if b.get("volume", 0) > 0]
            )
            if avg_vol <= 0:
                continue

            rel_volume = today_vol / avg_vol
            if rel_volume < volume_mult:
                continue

            breakout_pct = round((price - resistance) / resistance * 100, 2)

            candidates.append({
                "symbol": sym,
                "price": round(price, 2),
                "resistance": round(resistance, 2),
                "breakout_pct": breakout_pct,
                "rel_volume": round(rel_volume, 2),
                "avg_volume": round(avg_vol),
                "strategy": "breakout",
            })
        except Exception as e:
            log.warning("Breakout %s skip: %s", sym, e)

    candidates.sort(key=lambda x: x["breakout_pct"], reverse=True)
    log.info("Breakout screen: %d candidates", len(candidates))
    return candidates
