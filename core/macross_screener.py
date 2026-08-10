"""
Moving Average Crossover screener — 20/50-day SMA golden cross, volume-confirmed.

Universe: 80-stock S&P benchmark. Uses yfinance for historical OHLCV data
(1 year, ~252 bars) — no API key required, no rate limits.

Logic:
  1. Fetch daily bars for all universe symbols in ONE yfinance call
  2. For each: compute SMA-fast (20d) and SMA-slow (50d) for the full series
  3. Detect a golden cross: SMA-fast was <= SMA-slow, then crossed above it,
     within the last MACROSS_MAX_DAYS_SINCE_CROSS trading days
  4. Filter: price above SMA-slow (trend intact), volume on the cross day
     elevated vs. its own 20-day average (confirms real participation, not drift)
  5. Rank by (days since cross ascending, volume ratio descending) — freshest,
     most-confirmed crosses first
  6. Return top N candidates with metadata for trade sizing

Signal semantics — genuinely different from the other three active strategies:
  - Breakout reacts to price vs. a resistance level (50-day high)
  - Mean Reversion reacts to RSI/Bollinger extremes (a stretched rubber band)
  - Earnings Momentum reacts to a fundamental catalyst (an earnings beat)
  - This one reacts to trend structure itself: two moving averages agreeing
    that the intermediate trend just turned up. Pure trend-following, no
    extremity and no catalyst required.
  - Hold ~21 days with a 6% stop, time/trail-managed exit (no hard target) —
    the point of trend-following is to let a real trend run.

No TA-Lib. Pure-python indicators on yfinance OHLCV, same style as
core/meanrev_screener.py and core/breakout_screener.py.
"""

from __future__ import annotations

import logging

from core.config import (
    MACROSS_FAST_PERIOD,
    MACROSS_LIMIT,
    MACROSS_MAX_DAYS_SINCE_CROSS,
    MACROSS_MIN_AVG_VOLUME,
    MACROSS_MIN_PRICE,
    MACROSS_MIN_VOLUME_RATIO,
    MACROSS_SLOW_PERIOD,
    SP80_UNIVERSE,
)
from core.yf_utils import yf_download

log = logging.getLogger(__name__)

_N_BARS = 252  # ~1 year trading days


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _sma_series(closes: list[float], n: int) -> list[float | None]:
    """SMA(n) at every index of `closes` (None where insufficient history)."""
    out: list[float | None] = []
    running: list[float] = []
    for c in closes:
        running.append(c)
        out.append(_sma(running, n))
    return out


def _avg_volume(vols: list[float], n: int = 20) -> float:
    window = [v for v in vols[-n:] if v]
    return sum(window) / len(window) if window else 0.0


def _find_recent_cross(
    fast: list[float | None], slow: list[float | None], max_days_back: int
) -> int | None:
    """Index of the most recent bar where fast crossed from <= slow to >
    slow, if that happened within `max_days_back` bars of the end of the
    series. Returns None if no qualifying cross is found."""
    n = len(fast)
    for days_back in range(0, max_days_back + 1):
        i = n - 1 - days_back
        if i < 1:
            break
        f_now, s_now = fast[i], slow[i]
        f_prev, s_prev = fast[i - 1], slow[i - 1]
        if None in (f_now, s_now, f_prev, s_prev):
            continue
        if f_prev <= s_prev and f_now > s_now:
            return i
    return None


def _fetch_bars_batch(_symbols: list[str]) -> dict[str, list[dict]]:
    """Fetch daily bars from yfinance. Returns {symbol: [oldest→newest bars]}."""
    if not _symbols:
        return {}

    try:
        data = yf_download(
            _symbols,
            period="1y",
            progress=False,
            auto_adjust=False,
            group_by="ticker",
        )
    except Exception as e:
        log.warning("yfinance download failed: %s", e)
        return {}

    if data.empty:
        log.warning("yfinance returned empty data for %d symbols", len(_symbols))
        return {}

    out: dict[str, list[dict]] = {}
    min_bars = MACROSS_SLOW_PERIOD + MACROSS_MAX_DAYS_SINCE_CROSS + 1

    for sym in _symbols:
        try:
            cols = data.columns.get_level_values(0).unique()
            if sym not in cols:
                if "Close" in data.columns:
                    close_series = data["Close"][sym]
                    if close_series is None or close_series.isna().all():
                        continue
                else:
                    continue

            close_series = data[sym]["Close"].dropna()
            if len(close_series) < min_bars:
                continue

            high_series = data[sym]["High"].dropna()
            low_series = data[sym]["Low"].dropna()
            vol_series = data[sym]["Volume"].dropna()

            n = min(len(close_series), len(high_series), len(low_series), len(vol_series))
            if n < min_bars:
                continue

            bars = []
            for i in range(n):
                row_date = close_series.index[i]
                try:
                    bars.append(
                        {
                            "date": row_date.strftime("%Y-%m-%d"),
                            "high": float(high_series.iloc[i]) if i < len(high_series) else 0.0,
                            "low": float(low_series.iloc[i]) if i < len(low_series) else 0.0,
                            "close": float(close_series.iloc[i]),
                            "volume": float(vol_series.iloc[i]) if i < len(vol_series) else 0.0,
                        }
                    )
                except (TypeError, ValueError):
                    continue

            if len(bars) >= min_bars:
                out[sym] = bars
        except Exception as e:
            log.debug("yfinance bars %s: %s", sym, e)
            continue

    return out


def screen() -> list[dict]:
    """
    Run MA crossover screen. Returns candidates sorted by (days_since_cross
    ascending, volume_ratio descending) — freshest, most-confirmed crosses first.

    Candidate shape: {symbol, price, sma_fast, sma_slow, days_since_cross,
                      volume_ratio, avg_volume, score}
    """
    log.info(
        f"MACross screen: fetching {_N_BARS} days for {len(SP80_UNIVERSE)} symbols via yfinance"
    )
    bars_map = _fetch_bars_batch(SP80_UNIVERSE)
    log.info(f"  Got bars for {len(bars_map)} symbols")

    candidates: list[dict] = []

    for sym, bars in bars_map.items():
        try:
            closes = [b["close"] for b in bars if b.get("close")]
            vols = [b["volume"] for b in bars if b.get("volume") is not None]

            min_bars = MACROSS_SLOW_PERIOD + MACROSS_MAX_DAYS_SINCE_CROSS + 1
            if len(closes) < min_bars:
                continue

            price = closes[-1]
            if price < MACROSS_MIN_PRICE:
                continue

            avg_vol = _avg_volume(vols)
            if avg_vol < MACROSS_MIN_AVG_VOLUME:
                continue

            fast_series = _sma_series(closes, MACROSS_FAST_PERIOD)
            slow_series = _sma_series(closes, MACROSS_SLOW_PERIOD)

            cross_idx = _find_recent_cross(fast_series, slow_series, MACROSS_MAX_DAYS_SINCE_CROSS)
            if cross_idx is None:
                continue

            sma_fast = fast_series[-1]
            sma_slow = slow_series[-1]
            if sma_fast is None or sma_slow is None or sma_fast <= sma_slow:
                # Cross happened in the window but has since reversed — stale signal.
                continue

            # Trend confirmation: price must still be above the slow average.
            if price <= sma_slow:
                continue

            days_since_cross = (len(closes) - 1) - cross_idx

            # Volume confirmation: the cross day itself must show real participation.
            cross_vol = vols[cross_idx] if cross_idx < len(vols) else 0.0
            cross_day_avg_vol = _avg_volume(vols[: cross_idx + 1])
            volume_ratio = round(cross_vol / cross_day_avg_vol, 2) if cross_day_avg_vol > 0 else 0.0
            if volume_ratio < MACROSS_MIN_VOLUME_RATIO:
                continue

            sma_separation_pct = (sma_fast - sma_slow) / sma_slow * 100.0

            # Score: fresher crosses and stronger volume confirmation rank higher.
            score = (
                max(0.0, MACROSS_MAX_DAYS_SINCE_CROSS - days_since_cross) * 10.0
                + volume_ratio * 5.0
                + sma_separation_pct
            )

            candidates.append(
                {
                    "symbol": sym,
                    "price": price,
                    "sma_fast": round(sma_fast, 2),
                    "sma_slow": round(sma_slow, 2),
                    "sma_separation_pct": round(sma_separation_pct, 2),
                    "days_since_cross": days_since_cross,
                    "volume_ratio": volume_ratio,
                    "avg_volume": round(avg_vol),
                    "score": round(score, 2),
                }
            )
        except Exception as e:
            log.warning(f"MACross {sym}: %s", e)
            continue

    candidates.sort(key=lambda x: (x["days_since_cross"], -x["volume_ratio"]))
    top = candidates[:MACROSS_LIMIT]
    log.info(
        f"MACross: {len(top)}/{len(candidates)} candidates "
        f"({MACROSS_FAST_PERIOD}/{MACROSS_SLOW_PERIOD}d golden cross, volume-confirmed)"
    )
    for c in top:
        log.info(
            f"  {c['symbol']} cross {c['days_since_cross']}d ago | "
            f"SMA{MACROSS_FAST_PERIOD}={c['sma_fast']} > SMA{MACROSS_SLOW_PERIOD}={c['sma_slow']} "
            f"| vol_ratio={c['volume_ratio']} score={c['score']:.1f}"
        )
    return top
