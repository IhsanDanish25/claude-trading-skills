"""
50-day resistance breakout screener — FMP /stable/historical-price-eod.

A breakout is valid when:
  • Today's close is strictly above the 50-day trailing high (resistance).
  • Today's volume ≥ 1.5× the 50-day average daily volume (confirmation).

Score: breakout magnitude (% above resistance) × volume multiplier.

Data source: FMP /stable/historical-price-eod/full via core.fmp.get_daily_bars.
Default universe: config.WATCHLIST (easily overridden by callers).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _resistance_50(bars: list[dict]) -> float | None:
    """Highest close over bars[1:51] (excludes today, newest-first)."""
    window = bars[1:51]   # exclude today's bar; look at prior 50 sessions
    if len(window) < 10:
        return None
    try:
        return max(float(b["close"]) for b in window)
    except (KeyError, TypeError, ValueError):
        return None


def _avg_volume_50(bars: list[dict]) -> float | None:
    """Average daily volume over bars[1:51] (excludes today)."""
    window = bars[1:51]
    vols = [float(b["volume"]) for b in window if b.get("volume")]
    if not vols:
        return None
    return sum(vols) / len(vols)


def _score_breakout(pct_above: float, vol_mult: float) -> int:
    """0–100: rewards stronger breakouts and heavier volume."""
    mag_pts = min(int(pct_above * 10), 50)     # 0–50 for 0→5% above resistance
    vol_pts = min(int((vol_mult - 1.0) * 20), 50)  # 0–50 for 1→3.5× avg vol
    return max(0, mag_pts + vol_pts)


def screen(
    symbols: list[str] | None = None,
    vol_mult_min: float = 1.5,
    min_price: float = 5.0,
    min_avg_volume: float = 200_000.0,
) -> list[dict]:
    """Screen *symbols* for 50-day resistance breakouts with volume confirmation.

    Returns candidates sorted by score (highest first):
        {symbol, price, resistance_50, pct_above_resistance,
         avg_volume_50, current_volume, vol_mult, score}
    """
    from core.config import WATCHLIST
    from core.fmp import get_daily_bars

    syms = symbols if symbols is not None else WATCHLIST
    log.info("Breakout screen: %d symbols, vol_mult≥%.1f×", len(syms), vol_mult_min)

    candidates: list[dict] = []
    for sym in syms:
        try:
            bars = get_daily_bars(sym, days=80)   # newest-first; need ≥52 trading days
            if len(bars) < 52:
                continue

            price      = float(bars[0]["close"])
            cur_volume = float(bars[0].get("volume", 0))

            if price < min_price:
                continue

            resistance = _resistance_50(bars)
            if resistance is None or price <= resistance:
                continue   # no breakout

            avg_vol = _avg_volume_50(bars)
            if avg_vol is None or avg_vol < min_avg_volume:
                continue

            vol_mult = cur_volume / avg_vol if avg_vol > 0 else 0.0
            if vol_mult < vol_mult_min:
                continue   # breakout not volume-confirmed

            pct_above = round((price - resistance) / resistance * 100, 2)
            score     = _score_breakout(pct_above, vol_mult)

            candidates.append({
                "symbol":             sym,
                "price":              round(price, 2),
                "resistance_50":      round(resistance, 2),
                "pct_above_resistance": pct_above,
                "avg_volume_50":      round(avg_vol),
                "current_volume":     round(cur_volume),
                "vol_mult":           round(vol_mult, 2),
                "score":              score,
            })
        except Exception as e:
            log.debug("Breakout %s skip: %s", sym, e)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info("Breakout found %d candidates", len(candidates))
    return candidates
