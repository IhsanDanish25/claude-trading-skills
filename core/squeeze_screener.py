"""
Short Squeeze screener — yfinance batch bars + yfinance short interest.

Short squeezes happen when:
  1. Heavy short interest (>15% of float = many bears caught)         [yfinance]
  2. High days-to-cover (>3 = shorts can't exit quickly under pressure)  [computed]
  3. Upward momentum (forced covering drives further upside = the squeeze)  [yfinance]

Scoring:
  - SI as % of float: higher = more fuel, up to 40pts
  - DTC: higher = more trapped capital, up to 30pts
  - 20-day momentum: positive = catalyst likely, up to 30pts

Filter gates:
  - SI% > SQUEEZE_MIN_SI_PCT  (default 15%)
  - DTC > SQUEEZE_MIN_DTC     (default 3)
  - 20-day momentum > SQUEEZE_MIN_MOMENTUM  (positive, default 5%)
  - Price >= $5 (too-low price distorts SI data)

yfinance: short interest from .info attribute + 1 batch bars call (0 FMP).

FMP drain: 0 (fully replaced by yfinance).
"""
from __future__ import annotations

import logging

import yfinance as yf

from core.config import (
    SQUEEZE_HOLD_DAYS, SQUEEZE_STOP_PCT, SQUEEZE_SIZE_PCT,
    SQUEEZE_MIN_PRICE, SQUEEZE_MIN_SI_PCT, SQUEEZE_MIN_DTC,
    SQUEEZE_MIN_MOMENTUM, SQUEEZE_LIMIT, SP80_UNIVERSE,
)
from core.short_interest import get_short_interest

log = logging.getLogger(__name__)

_N_BARS = 60    # needs 20-day momentum lookback


# ── yfinance batch bars (called once at top of screen) ────────────────────────
def _fetch_bars_batch(symbols: list[str]) -> dict[str, list[dict]]:
    """Fetch daily bars from yfinance. Returns {sym: [oldest→newest]}. 0 FMP."""
    if not symbols:
        return {}
    try:
        data = yf.download(symbols, period="1y", progress=False,
                           auto_adjust=False, group_by="ticker")
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
            if len(cs) < 22:
                continue
            n = min(len(cs), len(data[sym]["High"]), len(data[sym]["Low"]))
            bars = []
            for i in range(n):
                bars.append({
                    "close": float(cs.iloc[i]),
                    "high":  float(data[sym]["High"].iloc[i]) if i < len(data[sym]["High"]) else 0.0,
                    "low":   float(data[sym]["Low"].iloc[i])  if i < len(data[sym]["Low"])  else 0.0,
                })
            out[sym] = bars
        except Exception:
            continue
    return out


def _squeeze_momentum(sym: str, bars_map: dict[str, list[dict]]) -> float:
    """20-bar momentum from pre-fetched yfinance bars. 0 FMP."""
    bars = bars_map.get(sym, [])
    if len(bars) < 22:
        return 0.0
    prices = [b["close"] for b in bars[-22:-1] if b.get("close")]
    if len(prices) < 22:
        return 0.0
    recent = prices[-1]
    past = prices[0]
    if past <= 0:
        return 0.0
    return (recent - past) / past * 100.0


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
    log.info("Squeeze screen: fetching short interest via yfinance (0 FMP)")

    candidates: list[dict] = []

    # Fetch short interest for all SP80 stocks via yfinance
    si_data = get_short_interest()
    log.info(f"  Got short-interest for {len(si_data)} symbols")

    # ── Prefetch all bars via yfinance (1 batch call, 0 FMP) ─────────────────
    bars_map = _fetch_bars_batch(SP80_UNIVERSE)
    log.info(f"  Prefetched yfinance bars for {len(bars_map)} symbols")

    for sym, si_row in si_data.items():
        try:
            si_pct = float(si_row.get("short_interest_pct") or 0)
            if si_pct < SQUEEZE_MIN_SI_PCT:
                continue

            dtc = float(si_row.get("days_to_cover") or 0)
            if dtc < SQUEEZE_MIN_DTC:
                continue

            # inst_own not available from yfinance SI — use proxy: 50% default
            # (conservative; DTC gating is the tighter filter anyway)
            inst_own = 50.0

            # Price from yfinance bars (0 FMP)
            bars = bars_map.get(sym, [])
            price = bars[-1]["close"] if bars else 0.0
            if price < SQUEEZE_MIN_PRICE:
                continue

            # ── 20-day momentum from yfinance bars ─────────────────────────
            momentum = _squeeze_momentum(sym, bars_map)
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
                "short_interest_absolute":   si_row.get("short_interest", 0),
                "days_to_cover":             round(dtc, 1),
                "institutional_ownership":    round(inst_own, 1),
                "momentum_pct":               round(momentum, 2),
                "score":                      round(total_score, 1),
                "si_score":                   round(si_score, 1),
                "dtc_score":                  round(dtc_score, 1),
                "mom_score":                  round(mom_score, 1),
            })
        except Exception as e:
            log.debug("Squeeze %s: %s", sym, e)
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