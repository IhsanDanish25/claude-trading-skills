"""
Mean-reversion screener — RSI<30 + Bollinger Band oversold, above SMA200.

Uses FMP /stable/ historical-price-eod for an 80-stock S&P sub-universe.
"""
from __future__ import annotations

import logging
import statistics

from core.fmp import get_daily_bars

log = logging.getLogger(__name__)

SP80 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "LLY",
    "UNH", "V", "XOM", "COST", "MA", "HD", "WMT", "NFLX", "PG", "JNJ",
    "ORCL", "ABBV", "CRM", "BAC", "AMD", "MRK", "CVX", "KO", "PEP", "ADBE",
    "TMO", "LIN", "ACN", "MCD", "CSCO", "WFC", "ABT", "GE", "DHR", "TXN",
    "IBM", "INTU", "AMGN", "QCOM", "NEE", "RTX", "PM", "VZ", "LOW", "UBER",
    "ISRG", "SPGI", "GS", "BKNG", "MS", "ELV", "COP", "CAT", "SYK", "MDT",
    "DE", "BLK", "ADP", "SCHW", "C", "GILD", "AMAT", "SBUX", "ADI", "MDLZ",
    "MMC", "VRTX", "LRCX", "AMT", "CI", "MU", "AXP", "PLD", "SO", "ETN",
]


def _rsi(closes: list[float], period: int = 14) -> float | None:
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
    return 100.0 - 100.0 / (1.0 + rs)


def _bollinger(closes: list[float], period: int = 20, num_std: float = 2.0) -> dict | None:
    if len(closes) < period:
        return None
    window = closes[:period]
    sma = statistics.mean(window)
    std = statistics.stdev(window)
    return {
        "sma": sma,
        "upper": sma + num_std * std,
        "lower": sma - num_std * std,
    }


def _sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return statistics.mean(closes[:period])


def screen_meanrev(
    symbols: list[str] | None = None,
    rsi_threshold: float = 30.0,
    bar_days: int = 250,
) -> list[dict]:
    """Find stocks with RSI<30, price below lower Bollinger Band, and above SMA200."""
    universe = symbols or SP80
    log.info("Mean-rev screen: %d symbols, RSI<%s", len(universe), rsi_threshold)

    candidates = []
    for sym in universe:
        try:
            bars = get_daily_bars(sym, days=bar_days)
            if len(bars) < 200:
                continue
            closes = [b["close"] for b in bars]

            sma200 = _sma(closes, 200)
            if sma200 is None or closes[0] < sma200:
                continue

            rsi = _rsi(closes)
            if rsi is None or rsi >= rsi_threshold:
                continue

            bb = _bollinger(closes)
            if bb is None or closes[0] > bb["lower"]:
                continue

            candidates.append({
                "symbol": sym,
                "price": closes[0],
                "rsi": round(rsi, 2),
                "bb_lower": round(bb["lower"], 2),
                "bb_sma": round(bb["sma"], 2),
                "sma200": round(sma200, 2),
                "strategy": "meanrev",
            })
        except Exception as e:
            log.warning("Mean-rev %s skip: %s", sym, e)

    candidates.sort(key=lambda x: x["rsi"])
    log.info("Mean-rev: %d candidates", len(candidates))
    return candidates
