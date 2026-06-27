"""Historical daily-bar cache + point-in-time slicer.

Pulls 2y+ of Alpaca IEX daily bars (the SAME feed production uses) for the
backtest universe and caches them on disk so re-runs are instant. Exposes a
`fake_fetch` that mimics `core.screener._fetch_bars` exactly but only ever
returns bars dated <= AS_OF — that monkeypatch is how the real screener and
composite get point-in-time data with no look-ahead.
"""
from __future__ import annotations

import datetime
import json
import logging
import math
import os

log = logging.getLogger("backtest.data")

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Index + sector ETFs the composite regime/sector context needs, plus SPY for
# the screener's RS-vs-SPY and the buy-and-hold benchmark.
INDEX_SYMBOLS = ["SPY", "QQQ", "IWM"]
SECTOR_ETFS = ["XLK", "XLC", "XLY", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLB", "XLRE"]

# ── Point-in-time cursor (set by the engine before each simulated decision) ────
AS_OF: datetime.date | None = None


def set_as_of(d: datetime.date) -> None:
    global AS_OF
    AS_OF = d


# ── Disk cache ────────────────────────────────────────────────────────────────
def _cache_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol.upper()}.json")


def load_cached(symbol: str) -> list[dict]:
    """Return cached bars oldest->newest, or [] if not cached."""
    p = _cache_path(symbol)
    if not os.path.exists(p):
        return []
    try:
        with open(p) as f:
            return json.load(f).get("bars", [])
    except (ValueError, OSError):
        return []


def _save_cached(symbol: str, bars: list[dict]) -> None:
    with open(_cache_path(symbol), "w") as f:
        json.dump({"symbol": symbol.upper(), "bars": bars}, f)


def fetch_and_cache(symbols: list[str], years: float = 2.2, force: bool = False) -> dict[str, list[dict]]:
    """Fetch daily IEX bars for `symbols` and cache them. Returns {sym: bars
    oldest->newest with date/open/high/low/close/volume}. Skips symbols already
    cached unless force=True."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY

    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=int(365 * years))

    out: dict[str, list[dict]] = {}
    todo = []
    for s in symbols:
        cached = load_cached(s)
        if cached and not force:
            out[s] = cached
        else:
            todo.append(s)

    log.info("cache: %d symbols cached, fetching %d from Alpaca IEX", len(out), len(todo))
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        try:
            resp = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                start=start, end=end, feed=DataFeed.IEX))
        except Exception as e:  # noqa: BLE001
            log.warning("cache: chunk fetch failed (%s) — symbols: %s", e, chunk)
            continue
        for sym in chunk:
            try:
                bars = resp[sym]
            except (KeyError, Exception):
                continue
            rows = [{
                "date":   b.timestamp.date().isoformat(),
                "open":   float(b.open),
                "high":   float(b.high),
                "low":    float(b.low),
                "close":  float(b.close),
                "volume": float(b.volume),
            } for b in bars]
            if rows:
                _save_cached(sym, rows)
                out[sym] = rows
    return out


# ── In-memory index for fast point-in-time access ─────────────────────────────
class BarStore:
    """Holds all cached series and answers point-in-time queries."""

    def __init__(self, series: dict[str, list[dict]]):
        # bars oldest->newest
        self.series = {s: bars for s, bars in series.items() if bars}
        self.by_date: dict[str, dict[str, dict]] = {}
        for s, bars in self.series.items():
            self.by_date[s] = {b["date"]: b for b in bars}

    def trading_calendar(self, anchor: str = "SPY") -> list[datetime.date]:
        return [datetime.date.fromisoformat(b["date"]) for b in self.series.get(anchor, [])]

    def bar_on(self, symbol: str, d: datetime.date) -> dict | None:
        return self.by_date.get(symbol, {}).get(d.isoformat())

    def first_date(self, symbol: str) -> datetime.date | None:
        bars = self.series.get(symbol)
        return datetime.date.fromisoformat(bars[0]["date"]) if bars else None

    def slice_asof(self, symbol: str, as_of: datetime.date, calendar_days: int) -> list[dict]:
        """Bars with (as_of - calendar_days) < date <= as_of, NEWEST-FIRST,
        projected to the {close,high,low,volume} shape the live fetcher returns."""
        lo = as_of - datetime.timedelta(days=calendar_days)
        rows = [
            {"close": b["close"], "high": b["high"], "low": b["low"], "volume": b["volume"]}
            for b in self.series.get(symbol, [])
            if lo < datetime.date.fromisoformat(b["date"]) <= as_of
        ]
        rows.reverse()
        return rows


# Global store the monkeypatched fetcher reads.
STORE: BarStore | None = None


def install_store(store: BarStore) -> None:
    global STORE
    STORE = store


def fake_fetch(symbols: list[str], days: int = 60) -> dict:
    """Drop-in for core.screener._fetch_bars. Mirrors its contract (newest-first
    {close,high,low,volume}; window = days*1.6 calendar days) but anchored at
    AS_OF, so callers only ever see point-in-time data."""
    assert STORE is not None, "BarStore not installed"
    assert AS_OF is not None, "AS_OF not set"
    calendar_days = int(math.ceil(days * 1.6))
    out: dict[str, list[dict]] = {}
    for sym in symbols:
        rows = STORE.slice_asof(sym, AS_OF, calendar_days)
        if rows:
            out[sym] = rows
    return out
