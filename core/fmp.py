from __future__ import annotations
"""
Financial Modeling Prep (FMP) data fetcher.
"""
import requests
import logging
from core.config import FMP_API_KEY

log = logging.getLogger(__name__)
BASE = "https://financialmodelingprep.com/api"


def _get(endpoint: str, params: dict = None) -> dict | list:
    params = params or {}
    params["apikey"] = FMP_API_KEY
    url = f"{BASE}{endpoint}"
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def get_market_breadth() -> dict:
    """SPY + QQQ trend + sector performance."""
    try:
        sectors = _get("/v3/sector-performance")
        spy     = _get("/v3/quote/SPY")
        qqq     = _get("/v3/quote/QQQ")
        iwm     = _get("/v3/quote/IWM")

        return {
            "spy_change_pct": spy[0].get("changesPercentage", 0) if spy else 0,
            "qqq_change_pct": qqq[0].get("changesPercentage", 0) if qqq else 0,
            "iwm_change_pct": iwm[0].get("changesPercentage", 0) if iwm else 0,
            "spy_price":      spy[0].get("price", 0) if spy else 0,
            "spy_trend":      "up" if spy and spy[0].get("changesPercentage", 0) > 0 else "down",
            "qqq_trend":      "up" if qqq and qqq[0].get("changesPercentage", 0) > 0 else "down",
            "sector_perf":    {s["sector"]: s["changesPercentage"] for s in sectors} if sectors else {},
        }
    except Exception as e:
        log.error(f"Breadth fetch fail: {e}")
        return {}


def get_quote(symbol: str) -> dict:
    try:
        data = _get(f"/v3/quote/{symbol}")
        return data[0] if data else {}
    except Exception as e:
        log.error(f"Quote {symbol} fail: {e}")
        return {}


def get_quotes(symbols: list[str]) -> dict:
    """Batch quote — returns {symbol: quote_dict}."""
    try:
        joined = ",".join(symbols)
        data   = _get(f"/v3/quote/{joined}")
        return {d["symbol"]: d for d in data} if data else {}
    except Exception as e:
        log.error(f"Batch quote fail: {e}")
        return {}


def get_daily_bars(symbol: str, days: int = 60) -> list[dict]:
    try:
        data = _get(f"/v3/historical-price-full/{symbol}", {"timeseries": days})
        return data.get("historical", [])
    except Exception as e:
        log.error(f"Bars {symbol} fail: {e}")
        return []


def get_news(tickers: list[str] = None, limit: int = 20) -> list[dict]:
    try:
        if tickers:
            return _get("/v3/stock_news", {"tickers": ",".join(tickers), "limit": limit})
        return _get("/v3/stock_news", {"limit": limit})
    except Exception as e:
        log.error(f"News fetch fail: {e}")
        return []


def get_economic_calendar(days_ahead: int = 3) -> list[dict]:
    import datetime
    today = datetime.date.today()
    end   = today + datetime.timedelta(days=days_ahead)
    try:
        return _get("/v3/economic_calendar", {
            "from": today.isoformat(),
            "to":   end.isoformat(),
        })
    except Exception as e:
        log.error(f"Calendar fail: {e}")
        return []


def get_gainers() -> list[dict]:
    try:
        return _get("/v3/stock_market/gainers") or []
    except Exception as e:
        log.error(f"Gainers fail: {e}")
        return []


def get_losers() -> list[dict]:
    try:
        return _get("/v3/stock_market/losers") or []
    except Exception as e:
        log.error(f"Losers fail: {e}")
        return []


def get_most_active() -> list[dict]:
    try:
        return _get("/v3/stock_market/actives") or []
    except Exception as e:
        log.error(f"Actives fail: {e}")
        return []


def get_52w_stats(symbol: str) -> dict:
    try:
        data = _get(f"/v3/quote/{symbol}")
        if not data:
            return {}
        q = data[0]
        return {
            "price":          q.get("price", 0),
            "year_high":      q.get("yearHigh", 0),
            "year_low":       q.get("yearLow", 0),
            "avg_volume":     q.get("avgVolume", 0),
            "volume":         q.get("volume", 0),
            "change_pct":     q.get("changesPercentage", 0),
            "market_cap":     q.get("marketCap", 0),
            "pct_from_high":  round(((q.get("price",0) - q.get("yearHigh",1)) / q.get("yearHigh",1)) * 100, 2),
        }
    except Exception as e:
        log.error(f"52w stats {symbol} fail: {e}")
        return {}
