"""
Mean Reversion screener — RSI < 30 + Bollinger Band oversold + above SMA200.

Universe: 80-stock S&P benchmark. Uses FMP /stable/ historical-price-eod.

Logic:
  1. Fetch daily bars for all universe symbols (300 bars ~ 14 months)
  2. For each: compute SMA-50, SMA-200, RSI(14), Bollinger Bands(20,2)
  3. Filter: price > SMA200 (trending market), RSI < threshold,
     price <= lower_bollinger_band + BB_THRESHOLD buffer
  4. Rank by RSI ascending (most oversold first)
  5. Return top N candidates with metadata for trade sizing

Signal semantics:
  - RSI < 30  : deep oversold — reversal probability elevated
  - At/near lower BB : price reached statistical lower extreme
  - Above SMA200   : in a healthy uptrend that should favor mean-reversion
  - Hold for ~14 days with tight 5% stop — expect 5-10% snap-back

No TA-Lib. Pure-python indicators on FMP OHLCV.
"""
from __future__ import annotations

import math
import logging
import datetime

from core.config import SP80_UNIVERSE, MEANREV_STOP_PCT, MEANREV_MIN_PRICE
from core.config import MEANREV_RSI_THRESHOLD, MEANREV_BB_THRESHOLD
from core.config import MEANREV_MIN_AVG_VOLUME, MEANREV_LIMIT
from core.fmp import _get, _stable

log = logging.getLogger(__name__)

_N_BARS = 300        # ~14 months of daily bars — enough for SMA200 + lookback
_RSI_PERIOD = 14
_SMA_PERIOD = 50
_SMA200_PERIOD = 200
_BB_PERIOD = 20
_BB_STD = 2.0


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _stddev(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    mean = sum(values[-n:]) / n
    variance = sum((v - mean) ** 2 for v in values[-n:]) / n
    return math.sqrt(variance)


def _rsi(closes: list[float], period: int = _RSI_PERIOD) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _bollinger_bands(closes: list[float], period: int = _BB_PERIOD,
                     std_mult: float = _BB_STD):
    sma = _sma(closes, period)
    sd = _stddev(closes, period)
    if sma is None or sd is None:
        return None, None, None
    lower = sma - std_mult * sd
    upper = sma + std_mult * sd
    return sma, upper, lower


def _avg_volume(bars: list[dict]) -> float:
    if not bars:
        return 0.0
    vols = [b.get("volume", 0) for b in bars[-20:] if b.get("volume")]
    return sum(vols) / len(vols) if vols else 0.0


def _momentum_pct(bars: list[dict], lookback: int = 20) -> float:
    if len(bars) < lookback + 1:
        return 0.0
    recent = bars[0]["close"]
    past = bars[lookback]["close"]
    if past <= 0:
        return 0.0
    return (recent - past) / past * 100.0


def _fetch_bars_batch(symbols: list[str]) -> dict[str, list[dict]]:
    """Fetch daily bars from FMP stable API. Returns {symbol: [newest-first bars]}."""
    today = datetime.date.today()
    from_ = today - datetime.timedelta(days=_N_BARS * 2)
    out: dict[str, list[dict]] = {}
    for sym in symbols:
        try:
            data = _get(f"{_stable}/historical-price-eod/full", {
                "symbol": sym,
                "from":    from_.isoformat(),
                "to":      today.isoformat(),
            })
            if not isinstance(data, list) or not data:
                continue
            rows = []
            for bar in data:
                if not isinstance(bar, dict):
                    continue
                try:
                    rows.append({
                        "date":   bar.get("date", ""),
                        "open":   float(bar.get("open", 0) or 0),
                        "high":   float(bar.get("high", 0) or 0),
                        "low":    float(bar.get("low", 0) or 0),
                        "close":  float(bar.get("close", 0) or 0),
                        "volume": float(bar.get("volume", 0) or 0),
                    })
                except (TypeError, ValueError):
                    continue
            if len(rows) >= 60:
                rows.reverse()
                out[sym] = rows
        except Exception as e:
            log.debug("FMP bars %s: %s", sym, e)
            continue
    return out


def screen() -> list[dict]:
    """
    Run mean-reversion screen. Returns candidates sorted by RSI (most oversold first).

    Candidate shape: {symbol, price, rsi, bb_lower, sma50, sma200, momentum,
                      avg_volume, bb_position, score}
    """
    log.info(f"MeanRev screen: fetching {_N_BARS} days for "
            f"{len(S&P80_UNIVERSE)} symbols via FMP /stable/")
    bars_map = _fetch_bars_batch(S&P80_UNIVERSE)
    log.info(f"  Got bars for {len(bars_map)} symbols")

    candidates: list[dict] = []

    for sym, bars in bars_map.items():
        try:
            closes = [b["close"] for b in bars if b.get("close")]
            highs = [b["high"] for b in bars if b.get("high")]
            lows = [b["low"] for b in bars if b.get("low")]

            if len(closes) < _SMA200_PERIOD + 1:
                continue

            price = closes[-1]  # most recent (newest-last after reverse)

            if price < MEANREV_MIN_PRICE:
                continue

            avg_vol = _avg_volume(bars)
            if avg_vol < MEANREV_MIN_AVG_VOLUME:
                continue

            # ── SMA200: must be above to confirm healthy trend ──────────────
            sma200 = _sma(closes, _SMA200_PERIOD)
            if sma200 is None or price <= sma200:
                continue

            # ── SMA50 ───────────────────────────────────────────────────────
            sma50 = _sma(closes, _SMA_PERIOD)

            # ── RSI(14) ────────────────────────────────────────────────────
            rsi = _rsi(closes)
            if rsi is None or rsi >= MEANREV_RSI_THRESHOLD:
                continue

            # ── Bollinger Bands ─────────────────────────────────────────────
            bb_sma, bb_upper, bb_lower = _bollinger_bands(closes)
            if bb_sma is None or bb_lower is None:
                continue

            # BB threshold buffer: negative = below lower band, 0 = at band
            if price > bb_lower + MEANREV_BB_THRESHOLD:
                continue

            # ── Momentum filter: already in a dip, not a crash ─────────────
            momentum = _momentum_pct(bars)
            bb_position = (price - bb_lower) / (bb_upper - bb_lower) if bb_upper != bb_lower else 0.0

            # Score: lower RSI = higher score
            score = max(0, MEANREV_RSI_THRESHOLD - rsi)

            candidates.append({
                "symbol":       sym,
                "price":        price,
                "rsi":          round(rsi, 1),
                "bb_lower":     round(bb_lower, 2),
                "bb_upper":     round(bb_upper, 2),
                "bb_sma":       round(bb_sma, 2),
                "bb_position":  round(bb_position * 100, 1),   # % up from lower band
                "sma50":        round(sma50, 2) if sma50 else 0.0,
                "sma200":       round(sma200, 2),
                "momentum_pct": round(momentum, 1),
                "avg_volume":   round(avg_vol),
                "score":        round(score, 2),
            })
        except Exception as e:
            log.warning(f"MeanRev {sym}: %s", e)
            continue

    candidates.sort(key=lambda x: (x["rsi"], -x["score"]))
    top = candidates[:MEANREV_LIMIT]
    log.info(f"MeanRev: {len(top)}/{len(candidates)} candidates "
             f"(RSI<{MEANREV_RSI_THRESHOLD}, above SMA200, near BB lower)")
    for c in top:
        log.info(f"  {c['symbol']} RSI={c['rsi']} BBpos={c['bb_position']:.0f}% "
                 f"momentum={c['momentum_pct']:+.1f}% score={c['score']:.1f}")
    return top