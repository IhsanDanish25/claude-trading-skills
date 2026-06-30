"""
Earnings-momentum screener — FMP /stable/earning_calendar.

Finds stocks that beat earnings 8-45 days ago and are still drifting up
(post-earnings announcement drift / PEAD continuation).
"""
from __future__ import annotations

import datetime
import logging

from core.fmp import _get, _STABLE, get_daily_bars

log = logging.getLogger(__name__)


def screen_earnings_momentum(
    min_days_since: int = 8,
    max_days_since: int = 45,
    min_drift_pct: float = 2.0,
    min_surprise_pct: float = 5.0,
    min_price: float = 10.0,
) -> list[dict]:
    """Screen for stocks still drifting up after an earnings beat."""
    today = datetime.date.today()
    from_date = today - datetime.timedelta(days=max_days_since + 5)
    to_date = today - datetime.timedelta(days=min_days_since)

    data = _get(f"{_STABLE}/earning_calendar", {
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
    })

    if not isinstance(data, list) or not data:
        log.info("Earnings-momentum screen: no calendar data")
        return []

    beats = []
    for row in data:
        actual = row.get("eps")
        estimated = row.get("epsEstimated")
        sym = row.get("symbol")
        report_date = row.get("date", "")

        if not sym or actual is None or estimated is None:
            continue
        try:
            actual = float(actual)
            estimated = float(estimated)
        except (TypeError, ValueError):
            continue

        if estimated == 0:
            continue

        surprise_pct = (actual - estimated) / abs(estimated) * 100.0
        if surprise_pct < min_surprise_pct:
            continue

        beats.append({
            "symbol": sym,
            "report_date": report_date,
            "actual_eps": actual,
            "estimated_eps": estimated,
            "surprise_pct": round(surprise_pct, 2),
        })

    log.info("Earnings-momentum: %d beats in window %s..%s", len(beats), from_date, to_date)

    seen = set()
    unique_beats = []
    for b in beats:
        if b["symbol"] not in seen:
            seen.add(b["symbol"])
            unique_beats.append(b)

    candidates = []
    for beat in unique_beats[:80]:
        sym = beat["symbol"]
        try:
            bars = get_daily_bars(sym, days=max_days_since + 10)
            if not bars or len(bars) < 5:
                continue

            price = bars[0]["close"]
            if price < min_price:
                continue

            report_date_str = beat["report_date"]
            report_price = None
            for b in bars:
                if b.get("date", "") <= report_date_str:
                    report_price = b["close"]
                    break

            if report_price is None or report_price <= 0:
                continue

            drift_pct = (price - report_price) / report_price * 100.0
            if drift_pct < min_drift_pct:
                continue

            days_since = (today - datetime.date.fromisoformat(report_date_str)).days

            candidates.append({
                "symbol": sym,
                "price": round(price, 2),
                "report_date": report_date_str,
                "surprise_pct": beat["surprise_pct"],
                "drift_pct": round(drift_pct, 2),
                "days_since_earnings": days_since,
                "report_price": round(report_price, 2),
                "strategy": "earnmom",
            })
        except Exception as e:
            log.warning("Earnings-momentum %s skip: %s", sym, e)

    candidates.sort(key=lambda x: x["drift_pct"], reverse=True)
    log.info("Earnings-momentum: %d candidates still drifting up", len(candidates))
    return candidates
