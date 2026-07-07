"""
Earnings-momentum screener — yfinance + Alpaca liquidity filter.

Signal source alternative to core.screener (VCP). Finds stocks that reported
earnings in the last few calendar days and beat EPS estimates by a wide margin,
then keeps only liquid names (via Alpaca OHLCV).

Data source: yfinance (free, no API key).
The Surprise(%) column from yfinance.Ticker.get_earnings_dates() is used directly.
"""
from __future__ import annotations

import datetime
import logging
import time

log = logging.getLogger(__name__)

_EARNINGS_LIMIT = 40  # ~10 years of quarterly data (4 per year)
_SLEEP_BETWEEN_SYMBOLS = 1.0  # polite sleep between yfinance calls


def compute_surprise_pct(actual, estimated) -> float | None:
    """EPS surprise as a percentage of the (absolute) estimate. None if either
    value is missing or the estimate is zero (surprise undefined)."""
    if actual is None or estimated is None:
        return None
    try:
        actual, estimated = float(actual), float(estimated)
    except (TypeError, ValueError):
        return None
    if estimated == 0:
        return None
    return (actual - estimated) / abs(estimated) * 100.0


def get_sp500_symbols() -> list[str]:
    """Fetch current S&P 500 constituents (~503 symbols).

    Sources tried in order:
      1. DataHub CSV (GitHub raw) — reliable, no bot-block
      2. Wikipedia HTML table — often Cloudflare-blocked
      3. Hardcoded top-200 by market cap — offline fallback
    """
    import pandas as pd

    # 1. DataHub (most reliable — plain CSV, no scraping)
    try:
        df = pd.read_csv(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
            "/main/data/constituents.csv"
        )
        syms = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        if len(syms) > 400:
            log.info("S&P 500 from DataHub: %d symbols", len(syms))
            return syms
    except Exception as e:
        log.debug("DataHub S&P 500 fetch failed: %s", e)

    # 2. Wikipedia
    try:
        table = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        syms = table["Symbol"].str.replace(".", "-", regex=False).tolist()
        if len(syms) > 400:
            log.info("S&P 500 from Wikipedia: %d symbols", len(syms))
            return syms
    except Exception as e:
        log.debug("Wikipedia S&P 500 fetch failed: %s", e)

    # 3. Hardcoded top-200 by market cap (offline fallback)
    log.warning("All S&P 500 sources failed — using hardcoded top-200")
    return [
        "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","JPM","LLY",
        "UNH","V","XOM","COST","MA","HD","WMT","NFLX","PG","JNJ","ORCL","ABBV",
        "CRM","BAC","AMD","MRK","CVX","KO","PEP","ADBE","TMO","LIN","ACN","MCD",
        "CSCO","WFC","ABT","GE","DHR","TXN","IBM","INTU","AMGN","QCOM","NEE",
        "RTX","PM","VZ","LOW","UBER","ISRG","SPGI","GS","BKNG","MS","ELV","COP",
        "CAT","SYK","MDT","T","DE","BLK","PFE","ADP","SCHW","C","GILD","AMAT",
        "SBUX","ADI","MDLZ","MMC","VRTX","LRCX","AMT","CI","MU","AXP","PLD",
        "SO","ETN","ZTS","CB","NOW","REGN","TJX","BSX","DUK","EOG","PH","KLAC",
        "HUM","PANW","ITW","MO","GEV","CME","USB","MMM","AOS","AFL","A","APD",
        "ABNB","AKAM","ALB","ARE","ALGN","LNT","ALL","GOOGL","GOOG","MO","AMCR",
        "AEE","AAL","AEP","AIG","AME","AMGN","APH","ADM","ANET","AJG","AIZ",
        "ATO","ADSK","AZO","AVB","AVY","AXON","BKR","BALL","BAX","BBY","BIO",
        "TECH","BLK","BX","BA","BCH","BKNG","BWA","BSX","BMY","AVGO","BR","BRO",
        "BLDR","BG","CHRW","CDNS","CZR","CPT","CPB","COF","CAH","KMX","CCL",
        "CARR","CTLT","CAT","CBRE","CDW","CE","CNC","CNP","CF","CHTR","CVX",
        "CMG","CB","CHD","CI","CINF","CTAS","CSCO","C","CFG","CLX","CME","CMS",
        "KO","CTSH","CL","CMCSA","CMA","CAG","COP","ED","STZ","CEG","COO","CPRT",
        "GLW","CPAY","CTVA","CSGP","CS","DHI","DHR","DRI","DVA","DAY","DECK",
    ]


def _fetch_fmp_earnings(symbol: str) -> list[dict] | None:
    """Fetch historical EPS surprises from FMP /api/v3/earnings-surprises.

    Returns same shape as the yfinance path: [{date, eps_estimate,
    reported_eps, surprise_pct}], or None on error.
    FMP_API_KEY must be set; silently returns None if absent.
    """
    import os
    import requests

    api_key = os.environ.get("FMP_API_KEY", "")
    if not api_key:
        return None
    try:
        r = requests.get(
            f"https://financialmodelingprep.com/api/v3/earnings-surprises/{symbol}",
            params={"apikey": api_key},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return None
        rows = []
        for item in data:
            date_str = item.get("date")
            actual = item.get("actualEarningResult")
            estimated = item.get("estimatedEarning")
            if not date_str or actual is None:
                continue
            try:
                reported = float(actual)
            except (TypeError, ValueError):
                continue
            try:
                eps_est = float(estimated) if estimated is not None else None
            except (TypeError, ValueError):
                eps_est = None
            rows.append({
                "date": date_str,
                "eps_estimate": eps_est,
                "reported_eps": reported,
                "surprise_pct": compute_surprise_pct(reported, eps_est),
            })
        return rows
    except Exception as e:
        log.warning("FMP earnings fetch failed for %s: %s", symbol, e)
        return None


def fetch_symbol_earnings(symbol: str, sleep: bool = True) -> list[dict] | None:
    """Fetch earnings history for one symbol via yfinance (no disk cache).

    Returns list of {date, eps_estimate, reported_eps, surprise_pct}, or
    an empty list if the symbol has no reported earnings (e.g. ETFs).
    Returns None on fetch error — callers should NOT cache None results so
    the next run retries.  Future reports (no Reported EPS yet) are skipped.
    Falls back to FMP /api/v3/earnings-surprises when yfinance fails.
    """
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).get_earnings_dates(limit=_EARNINGS_LIMIT)
        if df is None or df.empty:
            return []
        rows = []
        for ts, row in df.iterrows():
            try:
                date_str = ts.date().isoformat()
            except Exception:
                continue

            raw_reported = row.get("Reported EPS")
            try:
                reported = float(raw_reported)
                if reported != reported:  # NaN
                    continue
            except (TypeError, ValueError):
                continue

            try:
                eps_est = float(row.get("EPS Estimate"))
                if eps_est != eps_est:
                    eps_est = None
            except (TypeError, ValueError):
                eps_est = None

            try:
                surprise = float(row.get("Surprise(%)"))
                if surprise != surprise:
                    surprise = compute_surprise_pct(reported, eps_est)
            except (TypeError, ValueError):
                surprise = compute_surprise_pct(reported, eps_est)

            rows.append({
                "date": date_str,
                "eps_estimate": eps_est,
                "reported_eps": reported,
                "surprise_pct": surprise,
            })

        if sleep:
            time.sleep(_SLEEP_BETWEEN_SYMBOLS)
        return rows
    except Exception as e:
        log.warning("yfinance earnings fetch failed for %s: %s — trying FMP fallback", symbol, e)

    # FMP fallback — only reached when yfinance raises
    result = _fetch_fmp_earnings(symbol)
    if result is not None:
        log.info("FMP fallback succeeded for %s (%d rows)", symbol, len(result))
        return result
    return None  # do not cache — caller will retry next run


def _fetch_fmp_earnings_calendar(start_iso: str, end_iso: str) -> list[dict] | None:
    """Bulk earnings calendar from FMP for [start_iso, end_iso] — one API call
    covers every symbol that reported in the window (vs. one call per symbol).

    Returns raw FMP rows [{symbol, date, eps, epsEstimated, ...}], or None on
    error/missing key so the caller can fall back to the per-symbol scan.
    """
    import os
    import requests

    api_key = os.environ.get("FMP_API_KEY", "")
    if not api_key:
        return None
    try:
        r = requests.get(
            "https://financialmodelingprep.com/stable/earnings-calendar",
            params={"from": start_iso, "to": end_iso, "apikey": api_key},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            log.warning("FMP earnings-calendar: unexpected response shape")
            return None
        log.info("FMP earnings-calendar: %d rows for %s..%s", len(data), start_iso, end_iso)
        return data
    except Exception as e:
        log.warning("FMP bulk earnings-calendar fetch failed: %s — "
                    "falling back to per-symbol scan", e)
        return None


def _liquidity(symbols: list[str], lookback: int = 20) -> dict[str, dict]:
    """Latest price + average daily volume per symbol via Alpaca IEX bars.
    Returns {symbol: {"price": float, "avg_volume": float}}; absent if no data."""
    if not symbols:
        return {}
    from core import screener
    bars_map = screener.fetch_bars(list(dict.fromkeys(symbols)), days=max(lookback + 10, 30))
    out: dict[str, dict] = {}
    for sym, bars in bars_map.items():
        if not bars:
            continue
        vols = [b["volume"] for b in bars[:lookback] if b.get("volume")]
        out[sym] = {
            "price": bars[0]["close"],
            "avg_volume": sum(vols) / len(vols) if vols else 0.0,
        }
    return out


def screen_earnings(
    as_of: datetime.date | None = None,
    lookback_days: int = 7,
    min_surprise_pct: float = 10.0,
    min_price: float = 10.0,
    min_avg_volume: float = 500_000.0,
) -> list[dict]:
    """Stocks that reported in the last `lookback_days` calendar days and beat
    EPS estimates by >= `min_surprise_pct`, restricted to liquid names.

    Returns candidates sorted by surprise magnitude (biggest first):
        {symbol, surprise_pct, actual_eps, estimated_eps, report_date, price}

    Data source: FMP's bulk /stable/earnings-calendar endpoint (one API call
    for the whole lookback window, restricted to the S&P 500 universe). Falls
    back to the legacy per-symbol yfinance/FMP scan (via earnings_data disk
    cache) only when FMP_API_KEY is unset or the bulk call fails — that path
    is much slower on a cold cache (~1s/symbol x 503 symbols) so it should be
    the exception, not the norm, in live (non-backtest) runs.
    """
    end = as_of or datetime.date.today()
    start = end - datetime.timedelta(days=lookback_days)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    symbols = get_sp500_symbols()
    sp500_set = set(symbols)
    best: dict[str, dict] = {}

    calendar = _fetch_fmp_earnings_calendar(start_iso, end_iso)
    if calendar is not None:
        for item in calendar:
            sym = item.get("symbol")
            if not sym or sym not in sp500_set:
                continue
            d = item.get("date", "")
            sp = compute_surprise_pct(item.get("eps"), item.get("epsEstimated"))
            if not d or sp is None or sp < min_surprise_pct:
                continue
            if not (start_iso <= d <= end_iso):
                continue
            prev = best.get(sym)
            if prev is None or d >= prev["report_date"]:
                best[sym] = {
                    "symbol": sym,
                    "surprise_pct": round(sp, 2),
                    "actual_eps": item.get("eps"),
                    "estimated_eps": item.get("epsEstimated"),
                    "report_date": d,
                }
    else:
        from backtest_harness.earnings_data import get_symbol_earnings

        for sym in symbols:
            rows = get_symbol_earnings(sym)
            for row in rows:
                d = row.get("date", "")
                sp = row.get("surprise_pct")
                if not d or sp is None or sp < min_surprise_pct:
                    continue
                if not (start_iso <= d <= end_iso):
                    continue
                prev = best.get(sym)
                if prev is None or d >= prev["report_date"]:
                    best[sym] = {
                        "symbol": sym,
                        "surprise_pct": round(sp, 2),
                        "actual_eps": row.get("reported_eps"),
                        "estimated_eps": row.get("eps_estimate"),
                        "report_date": d,
                    }

    if not best:
        log.info("Earnings screen: 0 EPS beats >= %.0f%% in %s..%s", min_surprise_pct, start, end)
        return []

    liq = _liquidity(list(best.keys()))
    candidates = []
    for sym, c in best.items():
        info = liq.get(sym)
        if not info:
            continue
        if info["price"] <= min_price or info["avg_volume"] <= min_avg_volume:
            continue
        c["price"] = round(info["price"], 2)
        candidates.append(c)

    candidates.sort(key=lambda x: x["surprise_pct"], reverse=True)
    log.info("Earnings screen: %d beats, %d liquid candidates (%s..%s)",
             len(best), len(candidates), start, end)
    return candidates
