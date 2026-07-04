"""
VCP (Volatility Contraction Pattern) screener — ALPACA-ONLY.
No FMP calls = no rate limits. Uses Alpaca IEX bars (free).
"""
from __future__ import annotations
import datetime
import logging
import statistics

import pytz

from core.config import WATCHLIST, MIN_PRICE, MAX_PRICE, ET

log = logging.getLogger(__name__)

_data_client = None


def _client():
    global _data_client
    if _data_client is None:
        from alpaca.data.historical import StockHistoricalDataClient
        from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY
        _data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return _data_client


def _fetch_bars(symbols: list[str], days: int = 60) -> dict:
    """Batch daily bars from Alpaca IEX feed. Returns {symbol: [bars newest-first]}."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    now   = datetime.datetime.now(pytz.utc)
    start = now - datetime.timedelta(days=int(days * 1.6))
    out: dict = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i+100]
        try:
            resp = _client().get_stock_bars(
                StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                                 start=start, end=now, feed=DataFeed.IEX)
            )
            for sym in chunk:
                try:
                    bars = resp[sym]
                except (KeyError, Exception):
                    continue
                rows = [{
                    "close":  float(b.close),
                    "high":   float(b.high),
                    "low":    float(b.low),
                    "volume": float(b.volume),
                } for b in bars]
                rows.reverse()
                if rows:
                    out[sym] = rows
        except Exception as e:
            log.warning(f"Alpaca bars chunk failed: {e}")
            continue
    return out


def _adr(bars: list[dict], n: int = 10) -> float:
    if len(bars) < n:
        return 0.0
    recent = bars[:n]
    ranges = [(b["high"] - b["low"]) / b["low"] * 100 for b in recent if b["low"] > 0]
    return round(statistics.mean(ranges), 2) if ranges else 0.0


def _contraction_weeks(bars: list[dict], weeks: int = 5) -> int:
    week_ranges = []
    for i in range(0, min(len(bars), weeks * 5), 5):
        chunk = bars[i:i+5]
        if not chunk:
            break
        highs = [b["high"] for b in chunk]
        lows  = [b["low"]  for b in chunk]
        if highs and lows:
            week_ranges.append(max(highs) - min(lows))
    if len(week_ranges) < 2:
        return 0
    count = 0
    for i in range(len(week_ranges) - 1):
        if week_ranges[i] <= week_ranges[i + 1]:
            count += 1
        else:
            break
    return count


def _tight_closes(bars: list[dict], n: int = 5, threshold: float = 1.5) -> int:
    if len(bars) < n:
        return 0
    tight = 0
    for i in range(n - 1):
        if bars[i]["close"] > 0:
            chg = abs(bars[i]["close"] - bars[i+1]["close"]) / bars[i+1]["close"] * 100
            if chg < threshold:
                tight += 1
    return tight


def _return_pct_from_bars(bars: list[dict], lookback: int = 21) -> float | None:
    """1-month return % from bars (newest-first)."""
    if len(bars) < lookback:
        return None
    recent = bars[0]["close"]
    past   = bars[lookback - 1]["close"]
    if past <= 0:
        return None
    return round((recent - past) / past * 100, 2)


def screen(symbols: list[str] = None) -> list[dict]:
    """Screen symbols for VCP setups using Alpaca bars only.
    Computes RS-vs-SPY and gap% from the same bars — no FMP, no rate limits."""
    if symbols is None:
        symbols = WATCHLIST
    log.info(f"VCP screen [Alpaca]: {len(symbols)} symbols")

    fetch_syms = list(dict.fromkeys(symbols + ["SPY"]))
    bars_map = _fetch_bars(fetch_syms, days=60)
    spy_return = _return_pct_from_bars(bars_map.get("SPY", []))
    log.info(f"Alpaca returned bars for {len(bars_map)}/{len(fetch_syms)} symbols | SPY 1mo={spy_return}%")

    candidates = []
    for sym, bars in bars_map.items():
        if sym == "SPY":
            continue
        try:
            if len(bars) < 20:
                continue

            price  = bars[0]["close"]
            volume = bars[0]["volume"]

            if not (MIN_PRICE <= price <= MAX_PRICE):
                continue

            bar_vols = [b["volume"] for b in bars[:20] if b.get("volume")]
            avg_vol  = sum(bar_vols) / len(bar_vols) if bar_vols else 1
            if avg_vol < 100_000:
                continue

            rel_volume = round(volume / avg_vol, 2) if avg_vol > 0 else 0

            window_high   = max(b["high"] for b in bars)
            pct_from_high = round((price - window_high) / window_high * 100, 2)
            near_high     = pct_from_high >= -10

            adr               = _adr(bars)
            contraction_weeks = _contraction_weeks(bars)
            tight_closes_n    = _tight_closes(bars)

            stock_return = _return_pct_from_bars(bars)
            rs_vs_spy = round(stock_return - spy_return, 2) if (stock_return is not None and spy_return is not None) else None

            gap_pct = 0.0
            if len(bars) >= 2 and bars[1]["close"] > 0:
                gap_pct = round((bars[0]["close"] - bars[1]["close"]) / bars[1]["close"] * 100, 2)

            score = 0
            if near_high:                    score += 30
            if contraction_weeks >= 3:       score += 25
            elif contraction_weeks >= 2:     score += 15
            if tight_closes_n >= 4:          score += 20
            elif tight_closes_n >= 2:        score += 10
            if rel_volume >= 1.5:            score += 15
            if 0.5 <= adr <= 3.0:            score += 10

            candidates.append({
                "symbol":            sym,
                "price":             price,
                "rel_volume":        rel_volume,
                "adr_pct":           adr,
                "contraction_weeks": contraction_weeks,
                "tight_closes":      tight_closes_n,
                "pct_from_52w_high": pct_from_high,
                "near_52w_high":     near_high,
                "rs_vs_spy":         rs_vs_spy,
                "gap_pct":           gap_pct,
                "raw_score":         score,
                "score":             score,
            })
        except Exception as e:
            log.warning(f"VCP {sym} skip: {e}")
            continue

    candidates.sort(key=lambda x: x["raw_score"], reverse=True)
    log.info(f"VCP found {len(candidates)} candidates")
    return candidates
