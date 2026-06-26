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
import pytz
from core.config import FMP_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY

log = logging.getLogger(__name__)
_STABLE = "https://financialmodelingprep.com/stable"
_ET = pytz.timezone("America/New_York")

_cache: dict = {}
_CACHE_TTL = 300
_CACHE_MAX = 500
_warned_unavailable: set = set()
_alpaca_data_client = None


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
    if r.status_code == 429:
        # Rate-limited — degrade gracefully so the live loop never crashes.
        if "rate_limit_429" not in _warned_unavailable:
            log.warning("FMP rate-limited (HTTP 429) — returning [] (neutral) until quota resets")
            _warned_unavailable.add("rate_limit_429")
        return []
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
    q.setdefault("changesPercentage", q.get("changePercentage", 0))
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


def _alpaca_change_pct(symbol: str) -> float:
    """Daily change % via Alpaca IEX feed — for ETFs blocked on the FMP plan."""
    global _alpaca_data_client
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed
        if _alpaca_data_client is None:
            _alpaca_data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        now   = datetime.datetime.now(pytz.utc)
        start = now - datetime.timedelta(days=5)
        bars  = _alpaca_data_client.get_stock_bars(
            StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                             start=start, end=now, feed=DataFeed.IEX)
        )[symbol]
        if len(bars) >= 2:
            return round((bars[-1].close - bars[-2].close) / bars[-2].close * 100, 4)
    except Exception as e:
        log.warning("Alpaca change_pct %s: %s", symbol, e)
    return 0.0


def get_market_breadth() -> dict:
    """SPY (FMP stable) + QQQ/IWM (Alpaca, ETF-restricted on current FMP plan)."""
    try:
        spy     = _safe_quote("SPY")
        qqq_chg = _alpaca_change_pct("QQQ")
        iwm_chg = _alpaca_change_pct("IWM")
        return {
            "spy_change_pct": spy.get("changesPercentage", 0),
            "qqq_change_pct": qqq_chg,
            "iwm_change_pct": iwm_chg,
            "spy_price":      spy.get("price", 0),
            "spy_trend":      "up" if spy.get("changesPercentage", 0) > 0 else "down",
            "qqq_trend":      "up" if qqq_chg > 0 else "down",
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
    """Per-symbol quotes via stable API."""
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
    """Daily OHLCV bars, most-recent first."""
    try:
        today = datetime.date.today()
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


def get_next_earnings(symbol: str) -> str | None:
    """Next scheduled earnings date (YYYY-MM-DD) on/after today, or None."""
    try:
        data = _get(f"{_STABLE}/earnings", {"symbol": symbol})
        if not isinstance(data, list) or not data:
            return None
        today = datetime.date.today().isoformat()
        future = sorted(
            row["date"] for row in data
            if isinstance(row, dict) and row.get("date") and row["date"] >= today
        )
        return future[0] if future else None
    except Exception as e:
        log.error("Earnings %s fail: %s", symbol, e)
        return None


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
    """Live FMP screener — 500 liquid US stocks."""
    try:
        data = _get(f"{_STABLE}/company-screener", {
            "marketCapMoreThan": min_market_cap,
            "volumeMoreThan":    min_volume,
            "isActivelyTrading": "true",
            "isEtf":             "false",
            "limit":             limit,
        })
        if not isinstance(data, list):
            log.warning("Screener bad response: %s — falling back", type(data))
            return []
        symbols = [row["symbol"] for row in data if row.get("symbol")]
        log.info("Screener returned %d symbols", len(symbols))
        return symbols
    except Exception as e:
        log.warning("Screener failed: %s — returning []", e)
        return []
