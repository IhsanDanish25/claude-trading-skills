"""
Earnings Momentum screener — yfinance earnings history (live), FMP /stable/
earnings (backtest only — engine5 patches _get with point-in-time fixtures).

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

Live earnings source: yfinance Ticker.get_earnings_dates() — no FMP key
required, 0 FMP calls. Backtest keeps using FMP /stable/earnings via _get
(unchanged) since backtest_harness/satellite_signals.py's point-in-time
replica is what's actually validated, not this live-only screener.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os

from core import clock
from core.config import (
    EARNMOM_LIMIT,
    EARNMOM_LOOKBACK_DAYS,
    EARNMOM_MAX_DAYS_AGO,
    EARNMOM_MIN_AVG_VOLUME,
    EARNMOM_MIN_DRIFT_PCT,
    EARNMOM_MIN_PRICE,
    EARNMOM_MIN_SURPRISE_PCT,
    SP80_UNIVERSE,
)
from core.fmp import _STABLE as _stable
from core.fmp import _get
from core.yf_utils import yf_download

log = logging.getLogger(__name__)

# Live daily cache for /stable/earnings (earnings only change quarterly, so one
# fetch per symbol per day is plenty and keeps us well under the FMP quota).
_EARN_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "earnings_live"
)


def _fetch_earnings_yf(sym: str) -> list[dict]:
    """Recent reported-earnings history for one symbol via yfinance.

    Shaped like the old FMP /stable/earnings rows so the rest of screen()
    (surprise-pct calc, report-date window filter) is unchanged:
    [{date, epsActual, epsEstimated}, ...]. Rows with no reported EPS yet
    (future/upcoming quarter) are dropped.
    """
    try:
        import yfinance as yf

        df = yf.Ticker(sym).get_earnings_dates(limit=8)
    except Exception as e:  # noqa: BLE001
        log.debug("earnmom yfinance earnings %s: %s", sym, e)
        return []
    if df is None or df.empty:
        return []

    rows: list[dict] = []
    for ts, row in df.iterrows():
        reported = row.get("Reported EPS")
        if reported is None or (isinstance(reported, float) and math.isnan(reported)):
            continue
        estimate = row.get("EPS Estimate")
        has_estimate = estimate is not None and not (
            isinstance(estimate, float) and math.isnan(estimate)
        )
        rows.append(
            {
                "date": ts.strftime("%Y-%m-%d"),
                "epsActual": float(reported),
                "epsEstimated": float(estimate) if has_estimate else None,
            }
        )
    return rows


def _load_symbol_earnings(sym: str) -> list[dict]:
    """Full reported-earnings history for one symbol.

    Backtest: call FMP /stable/earnings straight through (engine5 patches
    _get to serve point-in-time rows) — unchanged. Live: fetch from
    yfinance (no FMP key needed), served from a per-day disk cache since
    earnings only change quarterly.
    """
    if clock.is_backtest():
        raw = _get(f"{_stable}/earnings", {"symbol": sym})
        return raw if isinstance(raw, list) else []

    today_s = clock.today().isoformat()
    path = os.path.join(_EARN_CACHE_DIR, f"{sym.upper()}.json")
    try:
        with open(path) as f:
            cached = json.load(f)
        if cached.get("fetched") == today_s:
            return cached.get("earnings", [])
    except (OSError, ValueError):
        pass

    rows = _fetch_earnings_yf(sym)
    try:
        os.makedirs(_EARN_CACHE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"fetched": today_s, "earnings": rows}, f)
    except OSError as e:
        log.debug("earnmom cache write %s failed: %s", sym, e)
    return rows


_N_BARS = 60  # need ~45 for drift + 20 for avg volume


def _fetch_bars_batch(symbols: list[str]) -> dict[str, list[dict]]:
    """Fetch daily bars from yfinance. Returns {symbol: [oldest→newest bars]}.

    1 yfinance call for all symbols — 0 FMP calls.
    """
    if not symbols:
        return {}
    try:
        data = yf_download(
            symbols,
            period="1y",
            progress=False,
            auto_adjust=False,
            group_by="ticker",
        )
    except Exception:
        return {}
    if data.empty:
        return {}

    out: dict[str, list[dict]] = {}
    for sym in symbols:
        try:
            cols = data.columns.get_level_values(0).unique()
            if sym not in cols:
                continue
            cs = data[sym]["Close"].dropna()
            if len(cs) < 5:
                continue

            n = min(
                len(cs), len(data[sym]["High"]), len(data[sym]["Low"]), len(data[sym]["Volume"])
            )
            bars = []
            for i in range(n):
                bar_date = cs.index[i].strftime("%Y-%m-%d")
                bars.append(
                    {
                        "date": bar_date,
                        "close": float(cs.iloc[i]),
                        "volume": float(data[sym]["Volume"].iloc[i])
                        if i < len(data[sym]["Volume"])
                        else 0.0,
                    }
                )
            out[sym] = bars  # oldest→newest
        except Exception:
            continue
    return out


def _get_price_yf(symbol: str) -> float:
    """Price from yfinance Ticker.fast_info (one call, no loop)."""
    try:
        import yfinance as yf

        return float(yf.Ticker(symbol).fast_info.last_price)
    except Exception:
        return 0.0


def _fetch_drift(sym: str, beat_date: str, bars_map: dict[str, list[dict]]) -> tuple[float, float]:
    """
    Get drift % and 20d avg volume from pre-fetched yfinance bars.
    Returns (drift_pct, avg_volume_20d). No FMP calls.
    """
    bars = bars_map.get(sym, [])
    if len(bars) < 5:
        return 0.0, 0.0

    # Find bar on/after beat_date
    beat_price = None
    for bar in bars:
        if bar["date"] and bar["date"] >= beat_date[:10]:
            beat_price = bar["close"]
            break

    if beat_price is None or beat_price <= 0:
        return 0.0, 0.0

    recent = bars[-1]["close"]
    if recent <= 0:
        return 0.0, 0.0

    drift_pct = (recent - beat_price) / beat_price * 100.0

    # 20-day avg volume
    vol_slice = bars[-20:]
    vols = [b["volume"] for b in vol_slice if b.get("volume")]
    avg_vol = sum(vols) / len(vols) if vols else 0.0

    return round(drift_pct, 2), round(avg_vol)


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
    today = clock.today()
    cutoff = today - datetime.timedelta(days=EARNMOM_LOOKBACK_DAYS)
    cutoff_s, today_s = cutoff.isoformat(), today.isoformat()
    candidates: list[dict] = []
    fetched = 0

    # Per-symbol /stable/earnings (the /earning_calendar batch endpoint is 404 on
    # our FMP tier). For each symbol keep the most recent REPORTED quarter within
    # the lookback window and derive the surprise % from actual vs. estimate.
    log.info(
        f"EarnMom screen: fetching per-symbol earnings via yfinance "
        f"(from={cutoff_s}, universe={len(SP80_UNIVERSE)})"
    )

    # ── Prefetch ALL bars via yfinance (one batch call, 0 FMP calls) ────────────
    bars_map: dict[str, list[dict]] = {}
    log.info("  Prefetching 1y bars for all %d symbols via yfinance...", len(SP80_UNIVERSE))
    bars_map = _fetch_bars_batch(SP80_UNIVERSE)
    log.info("  Got bars for %d symbols", len(bars_map))

    earnings_by_sym: dict[str, dict] = {}
    earnings_fetch_failed = 0
    for sym in SP80_UNIVERSE:
        try:
            rows = _load_symbol_earnings(sym)
        except Exception as e:  # noqa: BLE001
            earnings_fetch_failed += 1
            log.debug("EarnMom earnings %s: %s", sym, e)
            continue

        for row in rows:
            if not isinstance(row, dict):
                continue
            actual_eps = row.get("epsActual")
            if actual_eps is None:
                continue
            date_str = row.get("date")
            if not date_str:
                continue
            date_str = date_str[:10]
            # point-in-time window: reported on/before 'today', within lookback
            if not (cutoff_s <= date_str <= today_s):
                continue
            try:
                actual_eps = float(actual_eps)
            except (TypeError, ValueError):
                continue

            est_raw = row.get("epsEstimated")
            try:
                estimate = float(est_raw) if est_raw is not None else None
            except (TypeError, ValueError):
                estimate = None
            if estimate is not None and abs(estimate) > 1e-9:
                surprise_pct = (actual_eps - estimate) / abs(estimate) * 100.0
            else:
                surprise_pct = 0.0

            existing = earnings_by_sym.get(sym)
            if existing is None or date_str > existing["report_date"]:
                earnings_by_sym[sym] = {
                    "report_date": date_str,
                    "actual_eps": actual_eps,
                    "surprise_pct": round(surprise_pct, 4),
                }

    # Per-symbol fetch failures (402/403 tier restrictions, network errors,
    # etc.) are logged at debug level above and silently `continue`d — from
    # the outside, "half the universe failed to fetch" and "no one beat
    # earnings today" both just look like "EarnMom: 0 candidates". Surface
    # a summary at warning level so a real API problem doesn't hide behind
    # what reads as a quiet market day.
    if earnings_fetch_failed:
        log.warning(
            "EarnMom: %d/%d symbol earnings fetches failed (see debug log for "
            "per-symbol errors) — results may be incomplete",
            earnings_fetch_failed,
            len(SP80_UNIVERSE),
        )

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

            # 8-45 day drift window — filter BEFORE expensive _fetch_drift call
            if not (8 <= age_days <= EARNMOM_MAX_DAYS_AGO):
                continue

            # Early price filter from yfinance bars (0 FMP calls)
            bars = bars_map.get(sym, [])
            price = bars[-1]["close"] if bars else _get_price_yf(sym)
            if price < EARNMOM_MIN_PRICE:
                continue

            # ── Price drift from pre-fetched yfinance bars ──────────────────
            drift_pct, avg_vol = _fetch_drift(sym, report_date, bars_map)
            if avg_vol < EARNMOM_MIN_AVG_VOLUME:
                continue
            if drift_pct < EARNMOM_MIN_DRIFT_PCT:
                continue

            score = _drift_score(drift_pct, surprise_pct)

            candidates.append(
                {
                    "symbol": sym,
                    "price": round(price, 2),
                    "report_date": report_date,
                    "surprise_pct": round(surprise_pct, 2),
                    "actual_eps": info["actual_eps"],
                    "age_days": age_days,
                    "drift_pct": drift_pct,
                    "avg_volume": avg_vol,
                    "score": round(score, 1),
                }
            )
            fetched += 1
        except Exception as e:
            log.debug("EarnMom %s: %s", sym, e)
            continue

    candidates.sort(key=lambda x: -x["score"])
    top = candidates[:EARNMOM_LIMIT]
    log.info(
        f"EarnMom: {len(top)}/{len(candidates)} candidates "
        f"(beat 8-45d ago, drifted >{EARNMOM_MIN_DRIFT_PCT}%)"
    )
    for c in top:
        log.info(
            f"  {c['symbol']} surprise={c['surprise_pct']:+.1f}% "
            f"age={c['age_days']}d drift={c['drift_pct']:+.1f}% "
            f"score={c['score']:.0f}"
        )
    return top
