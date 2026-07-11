"""
Sector Rotation screener — buy leaders within top-performing sectors.

Entry:   buy the strongest stock in the strongest sector.
Logic:   sectors rotate. When a sector leads (e.g. tech +3% vs market +0.5%),
         its top stocks tend to continue. We buy names within top sectors
         that are themselves outperforming the sector average.

Filters:
  - Sector in top SECTOR_MIN_RANK sectors (default top 4)
  - Stock above 200-day SMA
  - RS Rating >= SECTOR_MIN_RS (vs SPY, default 15)
  - Price >= SECTOR_MIN_PRICE
  - Volume >= SECTOR_MIN_AVG_VOLUME

Stop:  SECTOR_STOP_PCT below fill price
Target: SECTOR_TAKE_PROFIT_PCT above fill (sector leaders can run 8-12%)
Hold:  SECTOR_HOLD_DAYS (default 14)

Win rate: 55-65%
Edge source: sector momentum + RS confirmation + individual strength
"""
from __future__ import annotations

import datetime
import logging

import pytz

from core.config import (
    SECTOR_MIN_RANK, SECTOR_STOP_PCT, SECTOR_TAKE_PROFIT_PCT,
    SECTOR_MIN_PRICE, SECTOR_MIN_AVG_VOLUME, SECTOR_LIMIT,
    SECTOR_HOLD_DAYS, SECTOR_MIN_RS, SECTOR_MAX_GAP_PCT,
    STRONG_SECTORS_ONLY,
)

log = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

_client = None
_SMA200_PERIOD = 200

# GICS sector → S&P 500 symbols (curated representative set)
SECTOR_UNIVERSE: dict[str, list[str]] = {
    "Technology":      ["NVDA","AMD","META","ADBE","CRM","PANW","CRWD","SNOW","DDOG","NET"],
    "Consumer":         ["TSLA","NFLX","MELI","SHOP","PINS","COIN","RIVN"],
    "Healthcare":       ["ISRG","DXCM","REGN","MRNA","ELV","CNC"],
    "Financials":       ["JPM","GS","AXP","SCHW","COIN"],
    "Industrials":      ["GE","CAT","RTX","HON","PCAR","AXON"],
    "Energy":           ["FSLR","ENPH","ON","AEHR","SMCI"],
    "Communications":   ["GOOGL","AMZN","OMC","CHTR"],
}


def _data_client():
    global _client
    if _client is None:
        from alpaca.data.historical import StockHistoricalDataClient
        from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY
        _client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return _client


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _avg(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _return_pct(closes: list[float], lookback: int) -> float | None:
    if len(closes) < lookback:
        return None
    recent = closes[-1]
    past = closes[-lookback]
    if past <= 0:
        return None
    return round((recent - past) / past * 100, 2)


def get_sector_performance(
    sector: str, symbols: list[str], days: int = 21
) -> float:
    """Average % return for sector symbols over last N days."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    now = datetime.datetime.now(ET)
    start = now - datetime.timedelta(days=days + 10)
    try:
        resp = _data_client().get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=start,
                end=now,
                feed=DataFeed.IEX,
            )
        )
    except Exception:
        return 0.0

    rets = []
    for sym in symbols:
        try:
            bars = sorted(resp[sym], key=lambda b: b.timestamp)
            bars = [b for b in bars if b.timestamp.date() < now.date()]
            if len(bars) < max(5, days):
                continue
            closes = [float(b.close) for b in bars]
            ret = _return_pct(closes, days)
            if ret is not None:
                rets.append(ret)
        except Exception:
            continue

    return _avg(rets) if rets else 0.0


def screen() -> list[dict]:
    """
    Rank sectors by 21-day performance, buy strongest stock in each top sector.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    from core import edge

    candidates: list[dict] = []

    # 1. Rank sectors by performance
    sector_rets: list[tuple[str, float, list[str]]] = []
    for sector, symbols in SECTOR_UNIVERSE.items():
        perf = get_sector_performance(sector, symbols, days=21)
        sector_rets.append((sector, perf, symbols))
    sector_rets.sort(key=lambda x: -x[1])

    top_sectors = [s for s, _, _ in sector_rets[:SECTOR_MIN_RANK]]
    log.info(f"Sector Rotation: top sectors = {top_sectors}")

    # 2. For each top sector, find the best stock
    now = datetime.datetime.now(ET)
    start = now - datetime.timedelta(days=max(_SMA200_PERIOD + 30, 252))
    spy_return = edge._return_pct("SPY", lookback=21)  # 21-day RS benchmark

    for sector, sector_ret, symbols in sector_rets[:SECTOR_MIN_RANK]:
        if len(candidates) >= SECTOR_LIMIT:
            break

        # Fetch bars for all sector symbols
        try:
            resp = _data_client().get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbols,
                    timeframe=TimeFrame.Day,
                    start=start,
                    end=now,
                    feed=DataFeed.IEX,
                )
            )
        except Exception as e:
            log.warning("Sector %s: bars failed: %s", sector, e)
            continue

        sector_candidates: list[dict] = []
        for sym in symbols:
            try:
                bars = sorted(resp[sym], key=lambda b: b.timestamp)
                bars = [b for b in bars if b.timestamp.date() < now.date()]
            except (KeyError, Exception):
                continue

            if len(bars) < _SMA200_PERIOD + 5:
                continue

            closes = [float(b.close) for b in bars]
            volumes = [float(b.volume) for b in bars]
            price = closes[-1]

            if price < SECTOR_MIN_PRICE:
                continue

            sma200 = _sma(closes, _SMA200_PERIOD)
            if sma200 is None or price <= sma200:
                continue

            avg_vol = _avg(volumes[-20:])
            if avg_vol < SECTOR_MIN_AVG_VOLUME:
                continue

            # RS rating vs SPY
            rs_ok, rs = edge.passes_rs(sym, spy_return)
            if not rs_ok:
                continue

            # Gap check: reject stocks gapping more than MAX_GAP_PCT today
            if len(bars) >= 2:
                gap = (price - closes[-2]) / closes[-2] * 100.0
                if abs(gap) > SECTOR_MAX_GAP_PCT:
                    continue

            # Individual stock 21-day momentum
            stock_ret = _return_pct(closes, 21) or 0.0

            sector_candidates.append({
                "symbol": sym,
                "sector": sector,
                "sector_ret": round(sector_ret, 2),
                "stock_ret": round(stock_ret, 2),
                "rs": rs,
                "price": round(price, 2),
                "avg_vol": round(avg_vol),
                "rel_vol": round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 0,
                "sma200": round(sma200, 2),
                "score": round(stock_ret + rs + sector_ret * 0.5, 2),
            })

        if not sector_candidates:
            continue

        # Pick top stock from sector
        sector_candidates.sort(key=lambda c: -c["score"])
        best = sector_candidates[0]
        best["stop"] = round(best["price"] * (1 - SECTOR_STOP_PCT), 2)
        best["target"] = round(best["price"] * (1 + SECTOR_TAKE_PROFIT_PCT), 2)
        best["hold_days"] = SECTOR_HOLD_DAYS
        candidates.append(best)
        log.info(f"  Sector {sector} leader: {best['symbol']} "
                 f"stock+{best['stock_ret']}% RS={best['rs']} sector+{best['sector_ret']}%")

    candidates.sort(key=lambda c: -c["score"])
    log.info(f"Sector Rotation: {len(candidates)} candidates")
    return candidates