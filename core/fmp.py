from __future__ import annotations
"""
Financial Modeling Prep (FMP) data fetcher.

Uses the /stable/ API base — the /api/v3/ endpoints return 403 on the
current FMP plan. Stable field names differ from v3 in a few places:
  - quote: changePercentage (not changesPercentage), no avgVolume field
  - historical: flat list (not dict with "historical" key)
Endpoints unavailable on this plan (economic calendar, news, gainers,
actives, screener) return [] with a one-time warning rather than raising.
"""
import datetime
import time
import requests
import logging
from core.config import FMP_API_KEY

log = logging.getLogger(__name__)
_STABLE = "https://financialmodelingprep.com/stable"

_cache: dict = {}
_CACHE_TTL = 300
_CACHE_MAX = 500
_warned_unavailable: set = set()


def _get(url: str, params: dict = None) -> dict | list:
    """GET with 5-min TTL cache. url must be a full URL."""
    if not FMP_API_KEY:
        log.error("FMP_API_KEY is empty — skipping API call to %s", url)
        return []
    params = params or {}
    params["apikey"] = FMP_API_KEY

    cache_key = (url, frozenset(params.items()))
    now = time.time()
    entry = _cache.get(cache_key)
    if entry and now - entry["ts"] < _CACHE_TTL:
        return entry["data"]

    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    if len(_cache) >= _CACHE_MAX:
        del _cache[min(_cache, key=lambda k: _cache[k]["ts"])]

    _cache[cache_key] = {"data": data, "ts": now}
    return data


def _unavailable(name: str) -> list:
    """Log once when a plan-restricted endpoint is called, return []."""
    if name not in _warned_unavailable:
        log.warning("FMP endpoint '%s' not available on current plan — returning []", name)
        _warned_unavailable.add(name)
    return []


def _quote(symbol: str) -> dict:
    """Single-symbol quote via stable API. Normalises field names to v3 shape."""
    data = _get(f"{_STABLE}/quote", {"symbol": symbol})
    if not data:
        return {}
    q = data[0] if isinstance(data, list) else data
    # stable uses changePercentage; normalise to changesPercentage for callers
    q.setdefault("changesPercentage", q.get("changePercentage", 0))
    # stable has no avgVolume — derive from bars if needed; default to volume
    q.setdefault("avgVolume", q.get("volume", 0))
    return q


def _safe_quote(symbol: str) -> dict:
    """Quote that returns {} on 402/403 without raising (ETF plan restriction)."""
    try:
        return _quote(symbol)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (402, 403):
            if symbol not in _warned_unavailable:
                log.warning("Quote %s unavailable on current FMP plan (HTTP %d)",
                            symbol, e.response.status_code)
                _warned_unavailable.add(symbol)
            return {}
        raise


def get_market_breadth() -> dict:
    """SPY + QQQ + IWM trend. QQQ/IWM require ETF plan; values default to 0
    if unavailable. Sector breakdown not available on current plan."""
    try:
        spy = _safe_quote("SPY")
        qqq = _safe_quote("QQQ")
        iwm = _safe_quote("IWM")
        return {
            "spy_change_pct": spy.get("changesPercentage", 0),
            "qqq_change_pct": qqq.get("changesPercentage", 0),
            "iwm_change_pct": iwm.get("changesPercentage", 0),
            "spy_price":      spy.get("price", 0),
            "spy_trend":      "up" if spy.get("changesPercentage", 0) > 0 else "down",
            "qqq_trend":      "up" if qqq.get("changesPercentage", 0) > 0 else "down",
            "sector_perf":    {},
        }
    except Exception as e:
        log.error("Breadth fetch fail: %s", e)
        return {}


def get_quote(symbol: str) -> dict:
    try:
        return _quote(symbol)
    except Exception as e:
        log.error("Quote %s fail: %s", symbol, e)
        return {}


def get_quotes(symbols: list[str]) -> dict:
    """Per-symbol quotes via stable API. Returns {symbol: quote_dict}.
    Stable does not allow comma-batching on the current plan, so calls
    are made individually; the TTL cache deduplicates repeated calls."""
    result: dict = {}
    for sym in symbols:
        try:
            q = _quote(sym)
            if q:
                result[sym] = q
        except Exception as e:
            log.debug("Quote %s skip: %s", sym, e)
    return result


def get_daily_bars(symbol: str, days: int = 60) -> list[dict]:
    """Daily OHLCV bars, most-recent first. Uses date range — the stable API
    ignores limit/timeseries params and returns all history otherwise."""
    try:
        today = datetime.date.today()
        # Add 50% calendar-day buffer to account for weekends/holidays
        start = today - datetime.timedelta(days=int(days * 1.5))
        data = _get(f"{_STABLE}/historical-price-eod/full", {
            "symbol": symbol,
            "from":   start.isoformat(),
            "to":     today.isoformat(),
        })
        return data if isinstance(data, list) else data.get("historical", [])
    except Exception as e:
        log.error("Bars %s fail: %s", symbol, e)
        return []


def get_news(tickers: list[str] = None, limit: int = 20) -> list[dict]:
    return _unavailable("stock-news")


def get_economic_calendar(days_ahead: int = 3) -> list[dict]:
    return _unavailable("economic-calendar")


def get_gainers() -> list[dict]:
    return _unavailable("market-gainers")


def get_losers() -> list[dict]:
    return _unavailable("market-losers")


def get_most_active() -> list[dict]:
    return _unavailable("market-most-active")


def get_52w_stats(symbol: str) -> dict:
    try:
        q = _quote(symbol)
        if not q:
            return {}
        year_high = q.get("yearHigh", 1) or 1
        return {
            "price":         q.get("price", 0),
            "year_high":     q.get("yearHigh", 0),
            "year_low":      q.get("yearLow", 0),
            "avg_volume":    q.get("avgVolume", 0),
            "volume":        q.get("volume", 0),
            "change_pct":    q.get("changesPercentage", 0),
            "market_cap":    q.get("marketCap", 0),
            "pct_from_high": round((q.get("price", 0) - year_high) / year_high * 100, 2),
        }
    except Exception as e:
        log.error("52w stats %s fail: %s", symbol, e)
        return {}


def get_screener_universe(
    min_market_cap: int = 2_000_000_000,
    min_volume: int = 500_000,
    limit: int = 500,
) -> list[str]:
    """Stock screener — not available on current FMP plan; returns []
    so core/screener.py falls back to config.WATCHLIST."""
    return _unavailable("company-screener")
