"""
Short Squeeze screener — FMP /stable/short-interest, SI > threshold + DTC + momentum.

Short squeezes happen when:
  1. Heavy short interest (>15% of float = many bears caught)
  2. High days-to-cover (>3 = shorts can't exit quickly under pressure)
  3. Upward momentum (forced covering drives further upside = the squeeze)

Scoring:
  - SI as % of float: higher = more fuel, up to 40pts
  - DTC: higher = more trapped capital, up to 30pts
  - 20-day momentum: positive = catalyst likely, up to 30pts

Filter gates:
  - SI% > SQUEEZE_MIN_SI_PCT  (default 15%)
  - DTC > SQUEEZE_MIN_DTC     (default 3)
  - 20-day momentum > SQUEEZE_MIN_MOMENTUM  (positive, default 5%)
  - Price >= $5 (too-low price distorts SI data)

FMP /stable/short-interest returns:
  [{symbol, shortInterest, float, daysToCover, institutionalOwnership, volume}, ...]
"""
from __future__ import annotations

import logging
import datetime
import math

from core.config import (
    SQUEEZE_HOLD_DAYS, SQUEEZE_STOP_PCT, SQUEEZE_SIZE_PCT,
    SQUEEZE_MIN_PRICE, SQUEEZE_MIN_SI_PCT, SQUEEZE_MIN_DTC,
    SQUEEZE_MIN_MOMENTUM, SQUEEZE_LIMIT,
)
from core.fmp import _get, _STABLE as _stable

log = logging.getLogger(__name__)

_N_BARS = 60    # needs 20-day momentum lookback


def _fetch_momentum(symbol: str) -> float:
    """Get 20-bar simple momentum % via FMP /stable/ historical-price-eod."""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=_N_BARS * 2)
    try:
        data = _get(f"{_stable}/historical-price-eod/full", {
            "symbol": symbol,
            "from":    start.isoformat(),
            "to":      today.isoformat(),
        })
        if not isinstance(data, list) or len(data) < 22:
            return 0.0
        # newest last
        prices = [float(b["close"]) for b in data[-22:-1] if b.get("close")]
        if len(prices) < 22:
            return 0.0
        recent = prices[-1]
        past = prices[0]
        if past <= 0:
            return 0.0
        return (recent - past) / past * 100.0
    except Exception:
        return 0.0


def _si_score(si_pct: float) -> float:
    """Score short interest % as potential fuel (max 40 pts)."""
    # Cap at 50% SI = 40pts, linear below
    return min(40.0, si_pct * 0.8)


def _dtc_score(dtc: float) -> float:
    """Score days-to-cover (max 30 pts). Caps at 10 DTC = 30pts."""
    return min(30.0, dtc * 3.0)


def _momentum_score(mom: float) -> float:
    """Score 20-day momentum as catalyst (max 30 pts, up to 20% momentum)."""
    return min(30.0, max(0.0, mom * 1.5))


def screen() -> list[dict]:
    """
    Run short-squeeze screen. Returns candidates sorted by total squeeze_score.

    Candidate shape: {symbol, price, short_interest_pct, days_to_cover,
                      institutional_ownership, momentum_pct, squeeze_score}
    """
    log.info("Squeeze screen: fetching short interest via FMP /stable/")
    today = datetime.date.today()
    start = today - datetime.timedelta(days=30)

    candidates: list[dict] = []

    # Fetch short interest for all known S&P symbols in batches
    symbols_batch = ",".join([
        "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN", "TSLA",
        "AVGO", "NFLX", "CRM", "ADBE", "JPM", "BAC", "GS", "MS",
        "XOM", "CVX", "COP", "EOG", "CAT", "GE", "HON", "BA",
        "PFE", "MRK", "LLY", "JNJ", "UNH", "ABBV",
        "HD", "MCD", "NKE", "SBUX", "TGT", "LOW", "WMT",
        "AMGN", "GILD", "BMY", "BMY",
        "DE", "MMM", "UPS", "FDX",
        "TJX", "ROST", "DG", "DLTR",
        "SPG", "PLD", "AMT", "CCI",
        "NEE", "DUK", "SO", "D",
    ])

    try:
        data = _get(f"{_stable}/short-interest", {
            "date": start.isoformat(),
        })
        if not isinstance(data, list):
            # Try without date filter as fallback
            data = _get(f"{_stable}/short-interest", {})
    except Exception as e:
        log.warning("FMP short-interest: %s", e)
        return []

    if not isinstance(data, list):
        log.warning("Squeeze screen: short-interest returned non-list %s", type(data))
        return []

    log.info(f"  Got {len(data)} short-interest records")

    for row in data:
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol")
        if not sym:
            continue
        try:
            si = float(row.get("shortInterest") or 0)
            fl = float(row.get("float") or 1)
            if fl <= 0:
                continue
            si_pct = (si / fl) * 100.0
            if si_pct < SQUEEZE_MIN_SI_PCT:
                continue

            dtc = float(row.get("daysToCover") or 0)
            if dtc < SQUEEZE_MIN_DTC:
                continue

            inst_own = float(row.get("institutionalOwnership") or 50)  # default 50%

            # Minimum price filter via FMP quote
            try:
                quote_data = _get(f"{_stable}/quote", {"symbol": sym})
                if isinstance(quote_data, list) and quote_data:
                    price = float(quote_data[0].get("price") or 0)
                elif isinstance(quote_data, dict):
                    price = float(quote_data.get("price") or 0)
                else:
                    price = 0.0
            except Exception:
                price = 0.0

            if price < SQUEEZE_MIN_PRICE:
                continue

            # ── 20-day momentum ───────────────────────────────────────────
            momentum = _fetch_momentum(sym)
            if momentum < SQUEEZE_MIN_MOMENTUM:
                continue

            # ── Scores ───────────────────────────────────────────────────
            si_score = _si_score(si_pct)
            dtc_score = _dtc_score(dtc)
            mom_score = _momentum_score(momentum)
            total_score = si_score + dtc_score + mom_score

            candidates.append({
                "symbol":                    sym,
                "price":                     round(price, 2),
                "short_interest_pct":        round(si_pct, 2),
                "short_interest_absolute":   round(si, 0),
                "days_to_cover":             round(dtc, 1),
                "institutional_ownership":    round(inst_own, 1),
                "momentum_pct":               round(momentum, 2),
                "score":                      round(total_score, 1),
                "si_score":                   round(si_score, 1),
                "dtc_score":                  round(dtc_score, 1),
                "mom_score":                  round(mom_score, 1),
            })
        except Exception as e:
            log.debug("Squeeze %s: %s", row.get("symbol", "?"), e)
            continue

    candidates.sort(key=lambda x: -x["score"])
    top = candidates[:SQUEEZE_LIMIT]
    log.info(f"Squeeze: {len(top)}/{len(candidates)} candidates "
             f"(SI>{SQUEEZE_MIN_SI_PCT}%, DTC>{SQUEEZE_MIN_DTC}, mom>{SQUEEZE_MIN_MOMENTUM}%)")
    for c in top:
        log.info(f"  {c['symbol']} SI={c['short_interest_pct']:.1f}% "
                 f"DTC={c['days_to_cover']:.1f}d mom={c['momentum_pct']:+.1f}% "
                 f"score={c['score']:.0f}")
    return top