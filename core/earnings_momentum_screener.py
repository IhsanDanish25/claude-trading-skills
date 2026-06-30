"""
Earnings-momentum screener — post-earnings drift with continued upward movement.

Uses FMP /stable/earning-calendar to find stocks that beat earnings 8-45 days ago,
then checks if price is still drifting up via /stable/historical-price-eod.
"""
from __future__ import annotations

import datetime
import logging

from core.fmp import _get, _STABLE

log = logging.getLogger(__name__)

_MIN_DAYS_SINCE = 8
_MAX_DAYS_SINCE = 45
_MIN_BEAT_PCT = 5.0


def screen_earnings_momentum(
    min_days: int = _MIN_DAYS_SINCE,
    max_days: int = _MAX_DAYS_SINCE,
    min_beat_pct: float = _MIN_BEAT_PCT,
) -> list[dict]:
    """Find stocks that beat earnings 8-45 days ago and are still drifting up.

    Uses FMP earning calendar with confirmed results, then validates:
    - EPS beat estimate by >= min_beat_pct
    - Current price > price on earnings date (still drifting up)
    - Current price > 10-day SMA (sustained momentum)

    Returns candidates sorted by drift percentage descending.
    """
    today = datetime.date.today()
    from_date = (today - datetime.timedelta(days=max_days)).isoformat()
    to_date = (today - datetime.timedelta(days=min_days)).isoformat()

    data = _get(f"{_STABLE}/earning-calendar", {
        "from": from_date,
        "to": to_date,
    })

    if not data or not isinstance(data, list):
        log.info("Earnings momentum: no calendar data from FMP")
        return []

    beats = []
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol")
        actual = row.get("eps")
        estimated = row.get("epsEstimated")
        report_date = row.get("date")

        if not sym or actual is None or estimated is None or not report_date:
            continue
        try:
            actual_f = float(actual)
            est_f = float(estimated)
        except (TypeError, ValueError):
            continue
        if est_f == 0:
            continue

        beat_pct = (actual_f - est_f) / abs(est_f) * 100.0
        if beat_pct < min_beat_pct:
            continue

        beats.append({
            "symbol": sym,
            "report_date": report_date,
            "actual_eps": actual_f,
            "estimated_eps": est_f,
            "beat_pct": round(beat_pct, 2),
        })

    if not beats:
        log.info("Earnings momentum: no EPS beats >= %.0f%% in %s..%s",
                 min_beat_pct, from_date, to_date)
        return []

    seen: set[str] = set()
    unique_beats = []
    for b in beats:
        if b["symbol"] not in seen:
            seen.add(b["symbol"])
            unique_beats.append(b)

    log.info("Earnings momentum: %d unique beats, checking drift...", len(unique_beats))

    candidates = []
    for beat in unique_beats:
        sym = beat["symbol"]
        try:
            bars = _get(f"{_STABLE}/historical-price-eod/full", {"symbol": sym})
            if not bars or not isinstance(bars, list) or len(bars) < 10:
                continue

            current_price = bars[0]["close"]
            closes_10d = [b["close"] for b in bars[:10] if b.get("close")]
            if len(closes_10d) < 10:
                continue
            sma10 = sum(closes_10d) / len(closes_10d)

            earnings_date = beat["report_date"]
            earnings_price = None
            for b in bars:
                if b.get("date", "") <= earnings_date:
                    earnings_price = b["close"]
                    break

            if earnings_price is None or earnings_price <= 0:
                continue

            drift_pct = (current_price - earnings_price) / earnings_price * 100.0

            if drift_pct <= 0:
                continue
            if current_price <= sma10:
                continue

            days_since = (today - datetime.date.fromisoformat(earnings_date)).days

            candidates.append({
                "symbol": sym,
                "price": round(current_price, 2),
                "earnings_price": round(earnings_price, 2),
                "drift_pct": round(drift_pct, 2),
                "beat_pct": beat["beat_pct"],
                "actual_eps": beat["actual_eps"],
                "estimated_eps": beat["estimated_eps"],
                "report_date": earnings_date,
                "days_since": days_since,
                "sma10": round(sma10, 2),
                "strategy": "earnmom",
            })
        except Exception as e:
            log.warning("earnmom %s skip: %s", sym, e)
            continue

    candidates.sort(key=lambda x: x["drift_pct"], reverse=True)
    log.info("Earnings momentum: %d drifting up from %d beats",
             len(candidates), len(unique_beats))
    return candidates
