"""
Breakout screener — yfinance batch OHLCV.

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

yfinance returns: flat list of {date, open, high, low, close, volume}
"""
from __future__ import annotations

import logging
import datetime
import statistics

from core.yf_utils import yf_download

from core.config import (
    BREAKOUT_HOLD_DAYS, BREAKOUT_STOP_PCT, BREAKOUT_SIZE_PCT,
    BREAKOUT_MIN_PRICE, BREAKOUT_VOL_MULT, BREAKOUT_MIN_AVG_VOLUME,
    BREAKOUT_LIMIT, SP80_UNIVERSE,
)

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


def _fetch_bars_batch(symbols: list[str]) -> dict[str, list[dict]]:
    """Fetch daily bars from yfinance (1 call, 0 FMP). Returns {sym: [oldest→newest]}."""
    if not symbols:
        return {}
    try:
        data = yf_download(symbols, period="1y", progress=False,
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
            if len(cs) < 55:
                continue
            n = min(len(cs),
                    len(data[sym]["High"]),
                    len(data[sym]["Low"]),
                    len(data[sym]["Volume"]))
            bars = []
            for i in range(n):
                dt = cs.index[i]
                bars.append({
                    "date":   dt.strftime("%Y-%m-%d"),
                    "open":   float(data[sym]["Open"].iloc[i])    if i < len(data[sym]["Open"])     else 0.0,
                    "high":   float(data[sym]["High"].iloc[i])    if i < len(data[sym]["High"])    else 0.0,
                    "low":    float(data[sym]["Low"].iloc[i])     if i < len(data[sym]["Low"])     else 0.0,
                    "close":  float(cs.iloc[i]),
                    "volume": float(data[sym]["Volume"].iloc[i])  if i < len(data[sym]["Volume"])  else 0.0,
                })
            out[sym] = bars
        except Exception:
            continue
    return out


def _avg_volume_h(bars: list[dict], lookback: int = 20) -> float:
    vols = [bars[i].get("volume", 0) for i in range(min(lookback, len(bars)))]
    return sum(vols) / len(vols) if vols else 0.0


def _compression_score(atr_pct: float) -> float:
    """Lower ATR = tighter compression = stronger breakout, up to 20 pts."""
    if atr_pct <= 2.0:
        return 20.0
    if atr_pct <= 4.0:
        return 10.0
    return 0.0


def _clearance_score(price: float, high_50: float) -> float:
    if high_50 <= 0:
        return 0.0
    clearance = (price - high_50) / high_50 * 100.0
    return min(40.0, max(0.0, clearance * 4.0))


def _volume_score(current_vol: float, avg_vol: float, mult: float) -> float:
    if avg_vol <= 0 or current_vol <= 0:
        return 0.0
    ratio = current_vol / avg_vol
    if ratio < mult:
        return 0.0
    return min(30.0, (ratio - mult) * 15.0 + 10.0)


def _atr_percentile(bars: list[dict]) -> float | None:
    if len(bars) < 55:
        return None
    n = 14
    atr_history = []
    for i in range(n, min(51, len(bars) - 1)):
        trs = []
        for j in range(max(0, i - n), i):
            high = bars[j]["high"]
            low  = bars[j]["low"]
            prev = bars[j - 1]["close"]
            tr = max(high - low, abs(high - prev), abs(low - prev))
            trs.append(tr)
        if len(trs) == n:
            atr_history.append(sum(trs) / n)
    if len(atr_history) < 10:
        return None
    current_atr = atr_history[-1]
    pct = sum(1 for v in atr_history if v < current_atr) / len(atr_history)
    return round(pct, 3)


def screen() -> list[dict]:
    """
    Run breakout screen. Returns candidates sorted by breakout_score.

    Candidate shape: {symbol, price, high_50d, volume_ratio, atr_pct,
                      clearance_pct, breakout_score}
    """
    log.info(f"Breakout screen: fetching {100} days for "
            f"breakout candidates, universe={len(SP80_UNIVERSE)}")

    candidates: list[dict] = []
    bars_map = _fetch_bars_batch(SP80_UNIVERSE)
    log.info(f"  Got bars for {len(bars_map)} symbols")

    for sym, bars in bars_map.items():
        try:
            if len(bars) < 55:
                continue

            closes  = [b["close"]  for b in bars]
            volumes = [b["volume"] for b in bars]
            highs   = [b["high"]   for b in bars]
            lows    = [b["low"]    for b in bars]

            price = closes[-1]
            if price < BREAKOUT_MIN_PRICE:
                continue

            vol_slice = bars[-20:]
            vols_20 = [b["volume"] for b in vol_slice]
            avg_vol = sum(vols_20) / len(vols_20) if vols_20 else 0.0
            if avg_vol < BREAKOUT_MIN_AVG_VOLUME:
                continue

            # ── 50-day high ───────────────────────────────────────────────
            high_50 = max(highs[-50:])
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

            # Fix 11: skip if ATR is in the upper 60% — not compressed enough
            ap = _atr_percentile(bars)
            base_duration_pct = ap if ap is not None else 0.3
            if base_duration_pct >= 0.40:
                log.debug("Breakout %s: ATR pct=%.1f%% (not compressed) — skipping",
                          sym, base_duration_pct * 100)
                continue

            candidates.append({
                "symbol":            sym,
                "price":             round(price, 2),
                "sma50":             round(sma50, 2),
                "high_50d":          round(high_50, 2),
                "clearance_pct":     round((price - high_50) / high_50 * 100, 2) if high_50 else 0,
                "volume_ratio":      round(vol_ratio, 2),
                "atr_pct":           round(atr_pct, 2),
                "base_duration_pct": round(base_duration_pct, 3),
                "avg_volume":        round(avg_vol),
                "current_volume":    round(current_vol),
                "score":             round(total, 1),
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