"""
Breakout screener — FMP /stable/ historical-price-eod.

Breakout = price breaks above 50-day resistance with volume confirmation.
Classic momentum trigger:
  - Stock compresses (tight range, low volatility)
  - Volume surges (1.5x+ average) = institutional accumulation
  - Price clears 50-day high = resistance becomes support

Scoring formula (max 100 pts):
  - Price clearance of 50d high:     40pts for clear break, up to 40
  - Volume surge ratio vs 20d avg:   up to 30pts (1.5x=10, 3x=30)
  - Strength of compression (ATR%):  up to 20pts (low ATR = tighter = stronger)
  - Recency bonus:                  up to 10pts if breakout confirmed today

Parameters:
  - BREAKOUT_VOL_MULT: minimum volume multiple to confirm (default 1.5x)
  - Price must be above SMA50 (confirm uptrend context - don't buy breakdowns)
  - Max position size: BREAKOUT_SIZE_PCT of portfolio

FMP returns: flat list of {date, open, high, low, close, volume}
"""
from __future__ import annotations

import logging
import datetime
import statistics

from core.config import (
    BREAKOUT_HOLD_DAYS, BREAKOUT_STOP_PCT, BREAKOUT_SIZE_PCT,
    BREAKOUT_MIN_PRICE, BREAKOUT_VOL_MULT, BREAKOUT_MIN_AVG_VOLUME,
    BREAKOUT_LIMIT, SP80_UNIVERSE,
)
from core.fmp import _get, _stable

log = logging.getLogger(__name__)

_N_BARS = 100     # need 50 + 20 + buffer


def _atr(bars: list[dict], n: int = 14) -> float:
    if len(bars) < n + 1:
        return 0.0
    trs = []
    for i in range(1, min(n + 1, len(bars))):
        high = bars[i]["high"]
        low = bars[i]["low"]
        prev_close = bars[i - 1]["close"]
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _avg_volume(bars: list[dict], lookback: int = 20) -> float:
    vols = [bars[i].get("volume", 0) for i in range(min(lookback, len(bars)))]
    return sum(vols) / len(vols) if vols else 0.0


def _clearance_score(price: float, high_50: float) -> float:
    """How far above the 50-day high, up to 40 pts."""
    if high_50 <= 0:
        return 0.0
    clearance = (price - high_50) / high_50 * 100.0
    # 0%=0pts, 5%=20pts, 10%+=40pts
    return min(40.0, max(0.0, clearance * 4.0))


def _volume_score(current_vol: float, avg_vol: float, mult: float) -> float:
    """Volume surge score, up to 30 pts."""
    if avg_vol <= 0 or current_vol <= 0:
        return 0.0
    ratio = current_vol / avg_vol
    # 1x = 0pts, 1.5x = 10pts, 2x = 20pts, 3x+ = 30pts
    if ratio < mult:
        return 0.0
    return min(30.0, (ratio - mult) * 15.0 + 10.0)


def _compression_score(atr_pct: float) -> float:
    """Lower ATR = tighter compression = stronger breakout, up to 20 pts.
    ATR% of price: <2% = 20pts, 2-4% = 10pts, >4% = 0pts."""
    if atr_pct < 0:
        return 0.0
    if atr_pct <= 2.0:
        return 20.0
    if atr_pct <= 4.0:
        return 10.0
    return 0.0


def screen() -> list[dict]:
    """
    Run breakout screen. Returns candidates sorted by breakout_score.

    Candidate shape: {symbol, price, high_50d, volume_ratio, atr_pct,
                      clearance_pct, breakout_score}
    """
    log.info(f"Breakout screen: fetching {_N_BARS} days for "
            f"breakout candidates, universe={len(S&P80_UNIVERSE)}")

    today = datetime.date.today()
    start = today - datetime.timedelta(days=_N_BARS * 2)
    candidates: list[dict] = []
    fetched = 0

    for sym in S&P80_UNIVERSE:
        try:
            data = _get(f"{_stable}/historical-price-eod/full", {
                "symbol": sym,
                "from":    start.isoformat(),
                "to":      today.isoformat(),
            })
            if not isinstance(data, list) or len(data) < 55:
                continue
            bars = []
            for bar in data:
                if not isinstance(bar, dict):
                    continue
                try:
                    bars.append({
                        "date":   bar.get("date", ""),
                        "open":   float(bar.get("open") or 0),
                        "high":   float(bar.get("high") or 0),
                        "low":    float(bar.get("low") or 0),
                        "close":  float(bar.get("close") or 0),
                        "volume": float(bar.get("volume") or 0),
                    })
                except (TypeError, ValueError):
                    continue
            bars.reverse()   # oldest first now

            if len(bars) < 55:
                continue
            fetched += 1

            closes  = [b["close"] for b in bars]
            volumes = [b["volume"] for b in bars]
            highs50 = [b["high"] for b in bars[-50:]]

            price = closes[-1]
            if price < BREAKOUT_MIN_PRICE:
                continue

            avg_vol = _avg_volume(bars[:20])
            if avg_vol < BREAKOUT_MIN_AVG_VOLUME:
                continue

            # ── 50-day high ───────────────────────────────────────────────
            high_50 = max(highs50)
            if price <= high_50:
                # Not yet broken out — skip unless within 1% (early breakout)
                clearance = (price - high_50) / high_50 * 100.0 if high_50 else 0
                if clearance < -1.0:
                    continue

            # ── SMA50 for context (should be in uptrend) ─────────────────
            sma50 = _sma(closes, 50)
            if sma50 is None or price < sma50:
                continue  # below SMA50 = not a breakout, it's a reversal

            # ── Volume confirmation ───────────────────────────────────────
            current_vol = volumes[-1] if volumes else 0
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0

            if vol_ratio < BREAKOUT_VOL_MULT:
                continue

            # ── ATR compression ────────────────────────────────────────────
            latest_atr = _atr(bars[-15:])
            atr_pct = (latest_atr / price * 100.0) if price > 0 else 0

            # ── Score components ─────────────────────────────────────────
            cl_score = _clearance_score(price, high_50)
            vol_score = _volume_score(current_vol, avg_vol, BREAKOUT_VOL_MULT)
            comp_score = _compression_score(atr_pct)
            total = cl_score + vol_score + comp_score

            candidates.append({
                "symbol":          sym,
                "price":           round(price, 2),
                "sma50":           round(sma50, 2),
                "high_50d":        round(high_50, 2),
                "clearance_pct":   round((price - high_50) / high_50 * 100, 2) if high_50 else 0,
                "volume_ratio":    round(vol_ratio, 2),
                "atr_pct":         round(atr_pct, 2),
                "avg_volume":      round(avg_vol),
                "current_volume":  round(current_vol),
                "score":           round(total, 1),
            })
        except Exception as e:
            log.debug("Breakout %s: %s", sym, e)
            continue

    candidates.sort(key=lambda x: -x["score"])
    top = candidates[:BREAKOUT_LIMIT]
    log.info(f"Breakout: {len(top)}/{len(candidates)} candidates "
             f"(above SMA50, vol>{BREAKOUT_VOL_MULT}x, cleared 50d high)")
    for c in top:
        log.info(f"  {c['symbol']} price=${c['price']:.2f} "
                 f"clearance={c['clearance_pct']:+.2f}% vol={c['volume_ratio']}x "
                 f"ATR={c['atr_pct']:.1f}% score={c['score']:.0f}")
    return top