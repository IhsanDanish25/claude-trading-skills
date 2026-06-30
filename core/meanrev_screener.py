"""
Mean-reversion screener — RSI<30 + Bollinger Band oversold + above SMA200.

Uses FMP /stable/historical-price-eod for an 80-stock S&P subset.
Gracefully returns [] on missing API key or rate limits.
"""
from __future__ import annotations

import logging
import statistics

from core.fmp import _get, _STABLE

log = logging.getLogger(__name__)

_UNIVERSE_SIZE = 80
_RSI_PERIOD = 14
_BB_PERIOD = 20
_BB_STD = 2.0
_SMA200_PERIOD = 200


def _get_sp80() -> list[str]:
    """Top 80 liquid S&P names — hardcoded to avoid an extra API call."""
    return [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
        "JPM", "LLY", "UNH", "V", "XOM", "COST", "MA", "HD", "WMT", "NFLX",
        "PG", "JNJ", "ORCL", "ABBV", "CRM", "BAC", "AMD", "MRK", "CVX",
        "KO", "PEP", "ADBE", "TMO", "LIN", "ACN", "MCD", "CSCO", "WFC",
        "ABT", "GE", "DHR", "TXN", "IBM", "INTU", "AMGN", "QCOM", "NEE",
        "RTX", "PM", "VZ", "LOW", "UBER", "ISRG", "SPGI", "GS", "BKNG",
        "MS", "ELV", "COP", "CAT", "SYK", "MDT", "T", "DE", "BLK", "PFE",
        "ADP", "SCHW", "C", "GILD", "AMAT", "SBUX", "ADI", "MDLZ", "MMC",
        "VRTX", "LRCX", "AMT", "CI", "MU", "AXP",
    ]


def _compute_rsi(closes: list[float], period: int = _RSI_PERIOD) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        delta = closes[i - 1] - closes[i]
        if delta > 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(delta))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_bollinger(closes: list[float], period: int = _BB_PERIOD,
                       num_std: float = _BB_STD) -> dict | None:
    if len(closes) < period:
        return None
    window = closes[:period]
    sma = statistics.mean(window)
    std = statistics.stdev(window) if len(window) > 1 else 0.0
    lower = sma - num_std * std
    upper = sma + num_std * std
    return {"sma": sma, "lower": lower, "upper": upper, "std": std}


def _sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[:period]) / period


def screen_meanrev() -> list[dict]:
    """Screen 80 S&P stocks for mean-reversion setups.

    Criteria: RSI < 30, price below lower Bollinger Band, price above SMA200.
    Returns candidates sorted by RSI ascending (most oversold first).
    """
    symbols = _get_sp80()
    candidates = []

    for sym in symbols:
        try:
            bars = _get(f"{_STABLE}/historical-price-eod/full", {"symbol": sym})
            if not bars or not isinstance(bars, list):
                continue
            closes = [b["close"] for b in bars if b.get("close")]
            if len(closes) < _SMA200_PERIOD:
                continue

            price = closes[0]
            rsi = _compute_rsi(closes)
            if rsi is None or rsi >= 30:
                continue

            bb = _compute_bollinger(closes)
            if bb is None or price >= bb["lower"]:
                continue

            sma200 = _sma(closes, _SMA200_PERIOD)
            if sma200 is None or price < sma200:
                continue

            candidates.append({
                "symbol": sym,
                "price": round(price, 2),
                "rsi": round(rsi, 2),
                "bb_lower": round(bb["lower"], 2),
                "bb_sma": round(bb["sma"], 2),
                "sma200": round(sma200, 2),
                "pct_below_bb": round((price - bb["lower"]) / bb["lower"] * 100, 2),
                "strategy": "meanrev",
            })
        except Exception as e:
            log.warning("meanrev %s skip: %s", sym, e)
            continue

    candidates.sort(key=lambda x: x["rsi"])
    log.info("Mean-reversion screen: %d candidates from %d symbols", len(candidates), len(symbols))
    return candidates
