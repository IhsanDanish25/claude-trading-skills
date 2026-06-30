"""
Short-squeeze screener — FMP /stable/short-interest.

Finds stocks with SI>15%, days-to-cover>3, and positive price momentum.
"""
from __future__ import annotations

import logging

from core.fmp import _get, _STABLE, get_daily_bars

log = logging.getLogger(__name__)


def _momentum_pct(bars: list[dict], lookback: int = 20) -> float | None:
    if len(bars) < lookback:
        return None
    recent = bars[0]["close"]
    past = bars[lookback - 1]["close"]
    if past <= 0:
        return None
    return round((recent - past) / past * 100, 2)


def screen_squeeze(
    min_si_pct: float = 15.0,
    min_days_to_cover: float = 3.0,
    min_momentum_pct: float = 0.0,
) -> list[dict]:
    """Screen for short-squeeze setups: high SI + long days-to-cover + positive momentum."""
    data = _get(f"{_STABLE}/short-interest", {"page": "0"})

    if not isinstance(data, list) or not data:
        log.info("Squeeze screen: no short-interest data returned")
        return []

    filtered = []
    for row in data:
        si_pct = 0.0
        try:
            si_pct = float(row.get("shortInterestPercentFloat", 0) or 0)
        except (TypeError, ValueError):
            try:
                si_pct = float(row.get("shortPercentFloat", 0) or 0)
            except (TypeError, ValueError):
                continue

        if si_pct < min_si_pct:
            continue

        try:
            dtc = float(row.get("daysToCover", 0) or 0)
        except (TypeError, ValueError):
            dtc = 0.0

        if dtc < min_days_to_cover:
            continue

        sym = row.get("symbol")
        if not sym:
            continue

        filtered.append({
            "symbol": sym,
            "si_pct": round(si_pct, 2),
            "days_to_cover": round(dtc, 2),
            "short_interest": row.get("shortInterest"),
            "date": row.get("date", ""),
        })

    log.info("Squeeze screen: %d pass SI/DTC filter, checking momentum...", len(filtered))

    candidates = []
    for item in filtered[:50]:
        sym = item["symbol"]
        try:
            bars = get_daily_bars(sym, days=30)
            if not bars:
                continue
            mom = _momentum_pct(bars)
            if mom is None or mom < min_momentum_pct:
                continue
            item["price"] = bars[0]["close"]
            item["momentum_20d"] = mom
            item["strategy"] = "squeeze"
            candidates.append(item)
        except Exception as e:
            log.warning("Squeeze %s momentum check skip: %s", sym, e)

    candidates.sort(key=lambda x: x["si_pct"], reverse=True)
    log.info("Squeeze screen: %d candidates", len(candidates))
    return candidates
