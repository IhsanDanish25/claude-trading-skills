"""
VCP (Volatility Contraction Pattern) screener.
Scores stocks for tight-base breakout setups.
"""
from __future__ import annotations
import logging
import statistics
from core.fmp import get_quotes, get_daily_bars, get_52w_stats
from core.config import WATCHLIST, MIN_PRICE, MAX_PRICE, MIN_RELATIVE_VOLUME

log = logging.getLogger(__name__)


def _adr(bars: list[dict], n: int = 10) -> float:
    """Average Daily Range % over last n bars."""
    if len(bars) < n:
        return 0.0
    recent = bars[:n]
    ranges = [(b["high"] - b["low"]) / b["low"] * 100 for b in recent if b["low"] > 0]
    return round(statistics.mean(ranges), 2) if ranges else 0.0


def _contraction_weeks(bars: list[dict], weeks: int = 5) -> int:
    """
    Count consecutive weeks of tightening weekly ranges.
    bars = daily, most-recent first.
    """
    # Group into weeks (5-day chunks)
    week_ranges = []
    for i in range(0, min(len(bars), weeks * 5), 5):
        chunk = bars[i:i+5]
        if not chunk:
            break
        highs = [b["high"] for b in chunk]
        lows  = [b["low"]  for b in chunk]
        if highs and lows:
            week_ranges.append(max(highs) - min(lows))

    if len(week_ranges) < 2:
        return 0

    # Count how many consecutive contractions from most recent
    count = 0
    for i in range(len(week_ranges) - 1):
        if week_ranges[i] <= week_ranges[i + 1]:
            count += 1
        else:
            break
    return count


def _tight_closes(bars: list[dict], n: int = 5, threshold: float = 1.5) -> int:
    """Count bars where close-to-close change < threshold %."""
    if len(bars) < n:
        return 0
    tight = 0
    for i in range(n - 1):
        if bars[i]["close"] > 0:
            chg = abs(bars[i]["close"] - bars[i+1]["close"]) / bars[i+1]["close"] * 100
            if chg < threshold:
                tight += 1
    return tight


def screen(symbols: list[str] = None) -> list[dict]:
    """
    Screen symbols for VCP setups.
    Returns list sorted by score (best first).
    """
    symbols = symbols or WATCHLIST
    log.info(f"VCP screen: {len(symbols)} symbols")

    # Batch quote
    quotes = get_quotes(symbols)
    candidates = []

    for sym in symbols:
        try:
            q = quotes.get(sym, {})
            if not q:
                continue

            price      = float(q.get("price", 0))
            avg_vol    = float(q.get("avgVolume", 1))
            volume     = float(q.get("volume", 0))
            year_high  = float(q.get("yearHigh", 1))
            year_low   = float(q.get("yearLow", 0))
            change_pct = float(q.get("changesPercentage", 0))

            # Basic filters
            if not (MIN_PRICE <= price <= MAX_PRICE):
                continue
            if avg_vol < 500_000:
                continue

            rel_volume = round(volume / avg_vol, 2) if avg_vol > 0 else 0
            pct_from_high = round((price - year_high) / year_high * 100, 2)

            # Get daily bars for pattern analysis
            bars = get_daily_bars(sym, days=60)
            if len(bars) < 20:
                continue

            adr               = _adr(bars)
            contraction_weeks = _contraction_weeks(bars)
            tight_closes_n    = _tight_closes(bars)
            near_high         = pct_from_high >= -10   # within 10% of 52w high

            # Score: weight each factor
            score = 0
            if near_high:                    score += 30
            if contraction_weeks >= 3:       score += 25
            elif contraction_weeks >= 2:     score += 15
            if tight_closes_n >= 4:          score += 20
            elif tight_closes_n >= 2:        score += 10
            if rel_volume >= 1.5:            score += 15
            if 0.5 <= adr <= 3.0:            score += 10   # not too volatile

            candidates.append({
                "symbol":             sym,
                "price":              price,
                "rel_volume":         rel_volume,
                "adr_pct":            adr,
                "contraction_weeks":  contraction_weeks,
                "tight_closes":       tight_closes_n,
                "pct_from_52w_high":  pct_from_high,
                "near_52w_high":      near_high,
                "raw_score":          score,
            })

        except Exception as e:
            log.warning(f"VCP {sym} skip: {e}")
            continue

    # Sort by raw score
    candidates.sort(key=lambda x: x["raw_score"], reverse=True)
    log.info(f"VCP found {len(candidates)} candidates")
    return candidates
