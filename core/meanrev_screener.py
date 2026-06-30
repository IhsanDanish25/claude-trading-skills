"""
Mean-reversion screener — RSI<30 + Bollinger Band oversold, price above SMA200.

Data source: FMP /stable/historical-price-eod/full (via core.fmp.get_daily_bars).
Universe: 80-stock curated S&P 500 liquid names (MEANREV_UNIVERSE).

Screen logic:
  1. Fetch 250 days of daily bars.
  2. Compute SMA(200), RSI(14), Bollinger Bands(20, 2σ) from chronological closes.
  3. Keep only: price > SMA200 AND RSI < threshold AND price <= lower BB.
  4. Score: combines RSI depth below threshold + magnitude below lower band.
"""
from __future__ import annotations

import logging
import statistics

log = logging.getLogger(__name__)

# 80 liquid S&P 500 names spanning all GICS sectors, sorted roughly by market cap.
MEANREV_UNIVERSE: list[str] = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM", "LLY", "UNH",
    "V", "XOM", "COST", "MA", "HD", "WMT", "PG", "JNJ", "ABBV", "CRM",
    "BAC", "AMD", "MRK", "CVX", "KO", "PEP", "ADBE", "TMO", "ACN", "MCD",
    "CSCO", "WFC", "ABT", "GE", "TXN", "IBM", "INTU", "AMGN", "QCOM", "NEE",
    "RTX", "PM", "VZ", "LOW", "UBER", "ISRG", "SPGI", "GS", "MS", "COP",
    "CAT", "SYK", "MDT", "DE", "BLK", "PFE", "ADP", "SCHW", "C", "GILD",
    "AMAT", "SBUX", "ADI", "MMC", "VRTX", "LRCX", "AMT", "CI", "MU", "AXP",
    "PLD", "SO", "ETN", "ZTS", "CB", "NOW", "REGN", "TJX", "BSX", "DUK",
]


# ── Pure compute helpers (oldest→newest closes) ───────────────────────────────

def _sma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return statistics.mean(closes[-n:])


def _rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's smoothed RSI using the last n+1 closes (oldest→newest)."""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(len(closes) - n, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def _bollinger(closes: list[float], n: int = 20, num_std: float = 2.0) -> dict | None:
    """Returns {mid, upper, lower} using the last *n* closes."""
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = statistics.mean(window)
    try:
        std = statistics.stdev(window)
    except statistics.StatisticsError:
        std = 0.0
    return {
        "mid":   round(mid, 4),
        "upper": round(mid + num_std * std, 4),
        "lower": round(mid - num_std * std, 4),
    }


# ── Public screen function ────────────────────────────────────────────────────

def screen(
    symbols: list[str] | None = None,
    rsi_threshold: float = 30.0,
    bb_std: float = 2.0,
    min_price: float = 5.0,
    min_avg_volume: float = 500_000.0,
) -> list[dict]:
    """Scan *symbols* for mean-reversion setups.

    Returns candidates sorted by composite score (best first):
        {symbol, price, rsi, sma200, bb_lower, bb_mid, bb_upper,
         pct_below_band, avg_volume, score}
    """
    from core.fmp import get_daily_bars

    syms = symbols if symbols is not None else MEANREV_UNIVERSE
    log.info("MeanRev screen: %d symbols, RSI<%s, BB(20,%sσ), SMA200", len(syms), rsi_threshold, bb_std)

    candidates: list[dict] = []
    for sym in syms:
        try:
            bars = get_daily_bars(sym, days=260)   # newest-first from FMP
            if len(bars) < 202:
                continue

            # Convert newest-first → chronological for indicator maths
            closes_chrono = [float(b["close"]) for b in reversed(bars)]
            price = closes_chrono[-1]

            if price < min_price:
                continue

            sma200 = _sma(closes_chrono, 200)
            if sma200 is None or price <= sma200:
                continue    # require uptrend context

            rsi = _rsi(closes_chrono, 14)
            if rsi is None or rsi >= rsi_threshold:
                continue

            bb = _bollinger(closes_chrono, 20, bb_std)
            if bb is None or price > bb["lower"]:
                continue    # price must be at or below lower band

            vols = [float(b.get("volume", 0)) for b in bars[:20]]
            avg_vol = sum(vols) / len(vols) if vols else 0.0
            if avg_vol < min_avg_volume:
                continue

            pct_below_band = round((price - bb["lower"]) / bb["lower"] * 100, 2)
            score = round(
                (rsi_threshold - rsi) * 1.5 + abs(min(pct_below_band, 0.0)) * 3.0, 1
            )

            candidates.append({
                "symbol":        sym,
                "price":         round(price, 2),
                "rsi":           rsi,
                "sma200":        round(sma200, 2),
                "bb_lower":      bb["lower"],
                "bb_mid":        bb["mid"],
                "bb_upper":      bb["upper"],
                "pct_below_band": pct_below_band,
                "avg_volume":    round(avg_vol),
                "score":         score,
            })
        except Exception as e:
            log.debug("MeanRev %s skip: %s", sym, e)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info("MeanRev found %d candidates", len(candidates))
    return candidates
