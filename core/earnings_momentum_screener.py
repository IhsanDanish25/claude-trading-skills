"""
Earnings Momentum screener — FMP /stable/ earning_calendar.

Earnings momentum: stocks that reported earnings 8-45 days ago and BEAT,
but have not yet re-rated — price is still drifting up as the market catches on.

Why 8-45 days?
  - Before day 8:  too soon — gap fill still playing out, thesis unconfirmed
  - Day 8-45:     "drift" phase — good earnings re-rate takes weeks to materialize
  - Beyond day 45: momentum fades, mean-reversion kicks in

Scoring: drift_pct weighted by surprise_magnitude + volume surge since beat.

Filters:
  - Surprise >= EARNMOM_MIN_SURPRISE_PCT (default 5%)
  - Earnings date within 8-45 calendar days ago
  - Price has drifted up since beat (drift > MIN_DRIFT_PCT, default 2%)
  - Above $10, avg volume > 500k (liquidity)

FMP /stable/ earning_calendar endpoint:
  [{date, symbol, epsd, epfA, epsChanged, revenueEstimated, revenueActual,
    surprisePercentage, fiscalPressure, displayPeriod}, ...]

Also fetches current FMP quote for price and volume drift confirmation.
"""
from __future__ import annotations

import datetime
import logging

from core.config import (
    EARNMOM_HOLD_DAYS, EARNMOM_STOP_PCT, EARNMOM_SIZE_PCT,
    EARNMOM_MIN_PRICE, EARNMOM_MIN_AVG_VOLUME, EARNMOM_MIN_SURPRISE_PCT,
    EARNMOM_LOOKBACK_DAYS, EARNMOM_MAX_DAYS_AGO, EARNMOM_MIN_DRIFT_PCT,
    EARNMOM_LIMIT, SP80_UNIVERSE,
)
from core.fmp import _get, _stable

log = logging.getLogger(__name__)

_N_BARS = 60     # need ~45 for drift + 20 for avg volume


def _fetch_drift(sym: str, beat_date: str) -> tuple[float, float]:
    """
    Get drift % and avg volume via FMP /stable/ historical-price-eod.

    Returns (drift_pct, avg_volume_20d). Drift is measured from beat_date close
    to most recent close. If too few bars, returns (0.0, 0.0).
    """
    today = datetime.date.today()
    start = today - datetime.timedelta(days=_N_BARS * 2)
    try:
        data = _get(f"{_stable}/historical-price-eod/full", {
            "symbol": sym,
            "from":   start.isoformat(),
            "to":     today.isoformat(),
        })
        if not isinstance(data, list) or len(data) < 5:
            return 0.0, 0.0

        rows = []
        for bar in data:
            if not isinstance(bar, dict):
                continue
            try:
                rows.append({
                    "date":   bar.get("date"),
                    "close":  float(bar.get("close") or 0),
                    "volume": float(bar.get("volume") or 0),
                })
            except (TypeError, ValueError):
                continue
        rows.reverse()  # oldest first

        # Find bar on or after beat_date
        beat_price = None
        for r in rows:
            if r["date"] and r["date"] >= beat_date[:10]:
                beat_price = r["close"]
                break

        if beat_price is None or beat_price <= 0:
            return 0.0, 0.0

        recent = rows[-1]["close"]
        if recent <= 0:
            return 0.0, 0.0

        drift_pct = (recent - beat_price) / beat_price * 100.0

        # 20-day avg volume
        vol_slice = rows[-20:]
        vols = [r["volume"] for r in vol_slice if r.get("volume")]
        avg_vol = sum(vols) / len(vols) if vols else 0.0

        return round(drift_pct, 2), round(avg_vol)
    except Exception:
        return 0.0, 0.0


def _drift_score(drift_pct: float, surprise_pct: float) -> float:
    """
    Combined momentum score: drift proves market re-rating is in progress.
    Up to 60 pts for drift (large drift = further to run), up to 40 pts for surprise.
    """
    drift_pts = min(60.0, max(0.0, drift_pct * 6.0))  # 10% drift = 60pts
    surprise_pts = min(40.0, max(0.0, surprise_pct * 2.0))  # 20% surprise = 40pts
    return drift_pts + surprise_pts


def screen() -> list[dict]:
    """
    Run earnings momentum screen. Returns candidates sorted by earnmom_score.

    Candidate shape: {symbol, price, report_date, surprise_pct, age_days,
                      drift_pct, avg_volume, earnmom_score}
    """
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=EARNMOM_LOOKBACK_DAYS)
    candidates: list[dict] = []
    fetched = 0

    # FMP earning_calendar returns ALL future + past earnings for all symbols.
    # So we fetch once for the whole universe.
    log.info(f"EarnMom screen: fetching earnings calendar via FMP /stable/ "
            f"(from={cutoff}, universe={len(S&P80_UNIVERSE)})")

    try:
        data = _get(f"{_stable}/earning_calendar", {
            "from": cutoff.isoformat(),
            "to":   today.isoformat(),
        })
    except Exception as e:
        log.warning("FMP earning_calendar: %s", e)
        return []

    if not isinstance(data, list):
        log.warning("EarnMom: earning_calendar returned %s", type(data))
        return []

    log.info(f"  Got {len(data)} earnings events")

    # Filter to past earnings (has actual reported EPS) for universe symbols
    earnings_by_sym: dict[str, dict] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol")
        if not sym or sym not in set(S&P80_UNIVERSE):
            continue

        # Only consider rows with actual reported EPS (not estimates)
        actual_eps = row.get("epsd") or row.get("epfd")
        if actual_eps is None:
            continue
        try:
            actual_eps = float(actual_eps)
        except (TypeError, ValueError):
            continue

        date_str = row.get("date")
        if not date_str:
            continue

        # Save the most-recently-reported beat for this symbol
        existing = earnings_by_sym.get(sym)
        if existing is None or date_str > existing["report_date"]:
            surprise_raw = row.get("surprisePercentage") or row.get("surprise_pct")
            try:
                surprise_pct = float(surprise_raw) if surprise_raw is not None else 0.0
            except (TypeError, ValueError):
                surprise_pct = 0.0

            earnings_by_sym[sym] = {
                "report_date":  date_str,
                "actual_eps":    actual_eps,
                "surprise_pct":  surprise_pct,
            }

    log.info(f"  Filtered to {len(earnings_by_sym)} symbols with reported beats in window")

    for sym, info in earnings_by_sym.items():
        try:
            report_date = info["report_date"]
            surprise_pct = info["surprise_pct"]

            if surprise_pct < EARNMOM_MIN_SURPRISE_PCT:
                continue

            # Compute age in days
            try:
                d = datetime.date.fromisoformat(report_date[:10])
                age_days = (today - d).days
            except (ValueError, TypeError):
                continue

            # 8-45 day drift window
            if not (8 <= age_days <= EARNMOM_MAX_DAYS_AGO):
                continue

            # Price and volume
            try:
                quote = _get(f"{_stable}/quote", {"symbol": sym})
                if isinstance(quote, list) and quote:
                    price = float(quote[0].get("price") or 0)
                elif isinstance(quote, dict):
                    price = float(quote.get("price") or 0)
                else:
                    price = 0.0
            except Exception:
                price = 0.0

            if price < EARNMOM_MIN_PRICE:
                continue

            # ── Price drift since beat ──────────────────────────────────────
            drift_pct, avg_vol = _fetch_drift(sym, report_date)
            if avg_vol < EARNMOM_MIN_AVG_VOLUME:
                continue
            if drift_pct < EARNMOM_MIN_DRIFT_PCT:
                continue

            score = _drift_score(drift_pct, surprise_pct)

            candidates.append({
                "symbol":        sym,
                "price":         round(price, 2),
                "report_date":   report_date,
                "surprise_pct":  round(surprise_pct, 2),
                "actual_eps":    info["actual_eps"],
                "age_days":      age_days,
                "drift_pct":     drift_pct,
                "avg_volume":    avg_vol,
                "score":         round(score, 1),
            })
            fetched += 1
        except Exception as e:
            log.debug("EarnMom %s: %s", sym, e)
            continue

    candidates.sort(key=lambda x: -x["score"])
    top = candidates[:EARNMOM_LIMIT]
    log.info(f"EarnMom: {len(top)}/{len(candidates)} candidates "
             f"(beat 8-45d ago, drifted >{EARNMOM_MIN_DRIFT_PCT}%)")
    for c in top:
        log.info(f"  {c['symbol']} surprise={c['surprise_pct']:+.1f}% "
                 f"age={c['age_days']}d drift={c['drift_pct']:+.1f}% "
                 f"score={c['score']:.0f}")
    return top