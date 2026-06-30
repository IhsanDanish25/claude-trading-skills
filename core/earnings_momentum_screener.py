"""
Post-earnings drift screener — FMP /stable/earning_calendar.

Finds stocks that:
  1. Reported earnings 8–45 days ago.
  2. Beat EPS estimates (positive surprise).
  3. Are still drifting up (current price > price at report date).

Score: eps_surprise_pct × 0.4 + drift_since_report_pct × 0.6 (scaled 0–100).

Data sources:
  • Earnings calendar — core.fmp.get_earnings_calendar → /stable/earning_calendar
  • Daily bars        — core.fmp.get_daily_bars        → /stable/historical-price-eod/full
"""
from __future__ import annotations

import datetime
import logging

log = logging.getLogger(__name__)


def _parse_surprise(row: dict) -> float | None:
    """Extract EPS surprise % from an earning_calendar row.

    FMP returns several possible key names depending on endpoint / plan tier.
    """
    for key in ("surprisePercentage", "epsSurprise", "surprise", "surprisePct"):
        val = row.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue

    # Fallback: compute from eps / epsEstimated
    for eps_key in ("eps", "actualEPS", "epsActual"):
        for est_key in ("epsEstimated", "estimatedEPS", "epsEstimate"):
            actual = row.get(eps_key)
            est    = row.get(est_key)
            if actual is None or est is None:
                continue
            try:
                a, e = float(actual), float(est)
                if e != 0:
                    return (a - e) / abs(e) * 100.0
            except (TypeError, ValueError, ZeroDivisionError):
                continue
    return None


def _price_at_report(bars: list[dict], report_date: str) -> float | None:
    """Find the close price on or just after *report_date* (newest-first bars)."""
    # Walk from oldest to newest to find first bar on/after report_date
    for bar in reversed(bars):
        bar_date = str(bar.get("date") or "")[:10]
        if bar_date >= report_date:
            try:
                return float(bar["close"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _score_earnmom(surprise_pct: float, drift_pct: float) -> int:
    """0–100 composite: EPS beat magnitude + drift momentum."""
    surprise_pts = min(max(surprise_pct * 0.8, 0.0), 40.0)   # 0–40
    drift_pts    = min(max(drift_pct    * 2.0, 0.0), 60.0)   # 0–60
    return int(surprise_pts + drift_pts)


def screen(
    as_of: datetime.date | None = None,
    lookback_min: int = 8,
    lookback_max: int = 45,
    min_surprise_pct: float = 0.0,
    min_drift_pct: float = 0.0,
    min_price: float = 5.0,
    min_avg_volume: float = 500_000.0,
) -> list[dict]:
    """Find stocks with post-earnings drift continuation.

    Returns candidates sorted by score (highest first):
        {symbol, report_date, surprise_pct, price_at_report, current_price,
         drift_pct, avg_volume, score}
    """
    from core.fmp import get_daily_bars, get_earnings_calendar

    today    = as_of or datetime.date.today()
    from_dt  = today - datetime.timedelta(days=lookback_max)
    to_dt    = today - datetime.timedelta(days=lookback_min)

    log.info(
        "EarnMom screen: window %s → %s (beat > %.0f%%, drift > %.0f%%)",
        from_dt, to_dt, min_surprise_pct, min_drift_pct,
    )

    cal = get_earnings_calendar(
        from_date=from_dt.isoformat(),
        to_date=to_dt.isoformat(),
    )
    if not cal:
        log.info("EarnMom: no earnings events in window")
        return []

    # Dedupe by symbol — keep highest-surprise event per symbol
    by_symbol: dict[str, dict] = {}
    for row in cal:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        surp = _parse_surprise(row)
        if surp is None or surp < min_surprise_pct:
            continue
        date_str = str(row.get("date") or "")[:10]
        if not date_str:
            continue
        prev = by_symbol.get(sym)
        if prev is None or surp > prev["surprise_pct"]:
            by_symbol[sym] = {"date": date_str, "surprise_pct": round(surp, 2)}

    if not by_symbol:
        log.info("EarnMom: 0 EPS beats found")
        return []

    candidates: list[dict] = []
    for sym, meta in by_symbol.items():
        try:
            bars = get_daily_bars(sym, days=60)
            if not bars:
                continue
            price = float(bars[0]["close"])
            if price < min_price:
                continue
            vols = [float(b.get("volume", 0)) for b in bars[:20]]
            avg_vol = sum(vols) / len(vols) if vols else 0.0
            if avg_vol < min_avg_volume:
                continue

            price_at_rpt = _price_at_report(bars, meta["date"])
            if price_at_rpt is None or price_at_rpt <= 0:
                continue

            drift_pct = round((price - price_at_rpt) / price_at_rpt * 100, 2)
            if drift_pct < min_drift_pct:
                continue

            score = _score_earnmom(meta["surprise_pct"], drift_pct)
            candidates.append({
                "symbol":          sym,
                "report_date":     meta["date"],
                "surprise_pct":    meta["surprise_pct"],
                "price_at_report": round(price_at_rpt, 2),
                "current_price":   round(price, 2),
                "drift_pct":       drift_pct,
                "avg_volume":      round(avg_vol),
                "score":           score,
            })
        except Exception as e:
            log.debug("EarnMom %s skip: %s", sym, e)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info("EarnMom found %d candidates", len(candidates))
    return candidates
