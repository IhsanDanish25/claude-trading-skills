"""
MA Pullback screener — 20/200 SMA trend & pullback.

Universe: 80-stock S&P benchmark. Uses yfinance for historical OHLCV data
(1 year, ~252 bars) — no API key required, no rate limits.

Logic:
  1. Fetch daily bars for all universe symbols in ONE yfinance call
  2. For each: compute SMA-20, SMA-200
  3. Filter:
     - Price > SMA200 (uptrend filter)
     - SMA20 > previous bar's SMA20 (rising 20 SMA)
     - Price crosses above SMA20 this bar (pullback bounce)
  4. Return candidates with metadata for trade sizing

Signal semantics:
  - Price above SMA200: in a healthy uptrend
  - Rising SMA20: intermediate trend strengthening
  - Price crossing above SMA20: pullback bounce off the 20-day MA
  - Exit: price crosses below SMA20 OR SMA200
  - Stop loss: hard 2% below entry price (MAPULLBACK_STOP_PCT)
  - No take-profit target — winners ride until MA-cross exit

No TA-Lib. Pure-python indicators on yfinance OHLCV.
"""
from __future__ import annotations

import logging
from typing import List, Dict, Any, Optional

from core.yf_utils import yf_download

from core.config import SP80_UNIVERSE
from core.config import MAPULLBACK_MIN_PRICE, MAPULLBACK_MAX_PRICE
from core.config import MAPULLBACK_MIN_AVG_VOLUME, MAPULLBACK_LIMIT

log = logging.getLogger(__name__)

_N_BARS = 252        # ~1 year trading days — enough for SMA200 + lookback
_SMA20_PERIOD = 20
_SMA200_PERIOD = 200


def _sma(values: List[float], n: int) -> Optional[float]:
    """Calculate simple moving average."""
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _fetch_bars_batch(symbols: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch daily bars from yfinance. Returns {symbol: [oldest→newest bars]}.

    yfinance returns ~252 trading days (1 year) in a single API call,
    no API key or rate-limit cost.
    """
    if not symbols:
        return {}

    # Fetch all symbols in one yfinance call (no API key needed)
    try:
        data = yf_download(
            symbols,
            period="1y",
            progress=False,
            auto_adjust=False,   # keep Close col (not Adj Close) for backward compat
            group_by="ticker",
        )
    except Exception as e:
        log.warning("yfinance download failed: %s", e)
        return {}

    if data.empty:
        log.warning("yfinance returned empty data for %d symbols", len(symbols))
        return {}

    out: Dict[str, List[Dict[str, Any]]] = {}

    for sym in symbols:
        try:
            # Multi-index access: data[sym]["Close"] etc.
            cols = data.columns.get_level_values(0).unique()
            if sym not in cols:
                # Try flat-column fallback (when group_by="ticker" fails)
                if "Close" in data.columns:
                    close_series = data["Close"][sym]
                    if close_series is None or close_series.isna().all():
                        continue
                else:
                    continue

            close_series = data[sym]["Close"].dropna()
            if len(close_series) < _SMA200_PERIOD + 1:
                continue

            open_series  = data[sym]["Open"].dropna()
            high_series  = data[sym]["High"].dropna()
            low_series   = data[sym]["Low"].dropna()
            vol_series   = data[sym]["Volume"].dropna()

            n = min(len(close_series), len(open_series), len(high_series), len(low_series), len(vol_series))
            if n < _SMA200_PERIOD + 1:
                continue

            bars = []
            for i in range(n):
                row_date = close_series.index[i]
                try:
                    bars.append({
                        "date":   row_date.strftime("%Y-%m-%d"),
                        "open":   float(open_series.iloc[i]),
                        "high":   float(high_series.iloc[i])   if i < len(high_series) else 0.0,
                        "low":    float(low_series.iloc[i])    if i < len(low_series)  else 0.0,
                        "close":  float(close_series.iloc[i]),
                        "volume": float(vol_series.iloc[i])    if i < len(vol_series)  else 0.0,
                    })
                except (TypeError, ValueError):
                    continue

            if len(bars) >= _SMA200_PERIOD + 1:
                out[sym] = bars   # oldest→newest
        except Exception as e:
            log.debug("yfinance bars %s: %s", sym, e)
            continue

    return out


def screen() -> List[Dict[str, Any]]:
    """
    Run MA Pullback screen. Returns candidates sorted by symbol (for now).

    Candidate shape: {symbol, price, sma20, sma200, prev_sma20,
                      volume, avg_volume}
    """
    log.info(f"MAPullback screen: fetching {_N_BARS} days for "
             f"{len(SP80_UNIVERSE)} symbols via yfinance")
    bars_map = _fetch_bars_batch(SP80_UNIVERSE)
    log.info(f"  Got bars for {len(bars_map)} symbols")

    candidates: List[Dict[str, Any]] = []

    for sym, bars in bars_map.items():
        try:
            closes = [b["close"] for b in bars if b.get("close") is not None]
            volumes = [b["volume"] for b in bars if b.get("volume") is not None]

            if len(closes) < _SMA200_PERIOD + 1:
                continue

            price = closes[-1]   # newest bar

            # Price filters
            if price < MAPULLBACK_MIN_PRICE or price > MAPULLBACK_MAX_PRICE:
                continue

            avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
            if avg_vol < MAPULLBACK_MIN_AVG_VOLUME:
                continue

            # Calculate SMAs
            sma20 = _sma(closes, _SMA20_PERIOD)
            sma200 = _sma(closes, _SMA200_PERIOD)

            if sma20 is None or sma200 is None:
                continue

            # Previous bar's SMA20 (for checking if rising)
            if len(closes) < _SMA20_PERIOD + 1:
                continue
            prev_closes = closes[:-1]  # all but last bar
            prev_sma20 = _sma(prev_closes, _SMA20_PERIOD)
            if prev_sma20 is None:
                continue

            # ENTRY CONDITIONS (all three must be true):
            # 1. Price is above the 200 SMA (uptrend filter)
            if price <= sma200:
                continue

            # 2. 20 SMA is rising (20 SMA > previous bar's 20 SMA)
            if sma20 <= prev_sma20:
                continue

            # 3. Price crosses above the 20 SMA this bar (pullback bounce)
            # Need to check if previous bar's price was <= previous bar's SMA20
            if len(closes) < 2:
                continue
            prev_close = closes[-2]
            prev_low = min(bars[-2]["low"] if bars[-2].get("low") else float('inf'),
                          bars[-1]["low"] if bars[-1].get("low") else float('inf'))
            # Simplified: check if previous close was <= previous SMA20
            if prev_close > prev_sma20:
                continue

            # All conditions met - add to candidates
            candidates.append({
                "symbol":       sym,
                "price":        round(price, 2),
                "sma20":        round(sma20, 2),
                "sma200":       round(sma200, 2),
                "prev_sma20":   round(prev_sma20, 2),
                "close":        round(price, 2),
                "volume":       bars[-1].get("volume", 0) if bars else 0,
                "avg_volume":   round(avg_vol),
            })

        except Exception as e:
            log.warning(f"MAPullback {sym}: %s", e)
            continue

    # Sort by symbol for consistency
    candidates.sort(key=lambda x: x["symbol"])
    top = candidates[:MAPULLBACK_LIMIT]
    log.info(f"MAPullback: {len(top)}/{len(candidates)} candidates "
             f"(price>SMA200, rising SMA20, price>SMA20 cross)")
    for c in top[:5]:  # Log first 5
        log.info(f"  {c['symbol']} price=${c['price']:.2f} "
                 f"SMA20={c['sma20']:.2f} SMA200={c['sma200']:.2f}")
    return top