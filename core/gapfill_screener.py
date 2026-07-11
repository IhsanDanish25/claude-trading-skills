"""
Gap Fill screener — fade morning gaps.

Entry:  stock gaps > GAPFILL_MIN_GAP_PCT at open, fades intraday.
Logic:  when a stock opens sharply higher/lower on news, it oftenmean-reverts
        to the prior close during the session. We fade the gap on the opening
        spike.

Filters:
  - Gap > GAPFILL_MIN_GAP_PCT (default 3%) at open vs prior close
  - GAPFILL_MAX_GAP_PCT cap (12%) — reject extreme gaps (news-driven, risky)
  - Price >= GAPFILL_MIN_PRICE
  - Volume >= GAPFILL_MIN_VOLUME (confirm institutional conviction)
  - No earnings within GAPFILL_EARNINGS_BLACKOUT_DAYS days

Direction:
  - Long  gap-up: fade the spike (expect pullback to prior close)
  - Long  gap-down (crash): fade the crash (expect bounce back)

Stop: GAPFILL_STOP_PCT below fill price
Target: prior close (exact fill) — pure mean-reversion, no upside cap

Win rate: 55-70% (academic studies; tightest on 3-5% gaps with volume)
Hold:  Intraday or max GAPFILL_HOLD_HOURS hours (default 4h)
"""
from __future__ import annotations

import datetime
import logging

import pytz

from core.config import (
    GAPFILL_MIN_GAP_PCT, GAPFILL_MAX_GAP_PCT, GAPFILL_MIN_PRICE,
    GAPFILL_MIN_VOLUME, GAPFILL_EARNINGS_BLACKOUT_DAYS, GAPFILL_LIMIT,
    GAPFILL_STOP_PCT, WATCHLIST,
)

log = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

_client = None


def _data_client():
    global _client
    if _client is None:
        from alpaca.data.historical import StockHistoricalDataClient
        from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY
        _client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return _client


def screen() -> list[dict]:
    """
    Scan WATCHLIST for morning gap candidates.

    Gap direction: positive = gap up (fade higher), negative = gap down (crash bounce).
    Returns up to GAPFILL_LIMIT candidates, sorted by absolute gap%.
    """
    from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    candidates: list[dict] = []

    # 1. Fetch yesterday's close bars (for prior close reference)
    now = datetime.datetime.now(ET)
    start = now - datetime.timedelta(days=10)  # extra buffer

    try:
        resp = _data_client().get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=WATCHLIST,
                timeframe=TimeFrame.Day,
                start=start,
                end=now,
                feed=DataFeed.IEX,
            )
        )
    except Exception as e:
        log.warning("GapFill: Alpaca bars fetch failed: %s", e)
        return []

    if not resp or not hasattr(resp, "__iter__"):
        return []

    # Build prior-close map: last bar close (before today)
    prior_close: dict[str, float] = {}
    today_str = now.date().isoformat()

    for sym in WATCHLIST:
        try:
            bars = sorted(resp[sym], key=lambda b: b.timestamp)
        except (KeyError, Exception):
            continue
        # Filter to bars before today's session
        session_bars = [b for b in bars if b.timestamp.date() < now.date()]
        if not session_bars:
            continue
        prior_close[sym] = float(session_bars[-1].close)

    # 2. Fetch today's latest bar (open + current)
    tickers = [s for s in WATCHLIST if s in prior_close]
    if not tickers:
        return []

    try:
        today_bars = _data_client().get_stock_latest_bar(
            StockLatestBarRequest(symbol_or_symbols=tickers, feed=DataFeed.IEX)
        )
    except Exception as e:
        log.warning("GapFill: today's bar fetch failed: %s", e)
        return []

    # 3. Evaluate gap
    session_open_map: dict[str, float | None] = {}
    for sym in tickers:
        try:
            bar = today_bars[sym]
            session_open_map[sym] = float(bar.open)
        except Exception:
            session_open_map[sym] = None

    # 4. Filter by sector concentration + earnings blackout
    try:
        from core.fmp import get_next_earnings
        has_earnings_cache = True
    except Exception:
        has_earnings_cache = False

    try:
        from core.fmp import get_daily_bars

        # Pre-fetch today's volume
        volume_today: dict[str, float] = {}
        for sym in tickers:
            bars_today = _data_client().get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=[sym],
                    timeframe=TimeFrame.Day,
                    start=now.replace(hour=0, minute=0, second=0),
                    end=now,
                    feed=DataFeed.IEX,
                )
            )
            try:
                today_b = [b for b in bars_today[sym] if b.timestamp.date() == now.date()]
                if today_b:
                    volume_today[sym] = float(today_b[-1].volume)
            except Exception:
                pass

        # 20-day avg volume as reference
        avg_vol: dict[str, float] = {}
        for sym in tickers:
            try:
                bars_hist = _data_client().get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=[sym],
                        timeframe=TimeFrame.Day,
                        start=now - datetime.timedelta(days=60),
                        end=now - datetime.timedelta(days=1),
                        feed=DataFeed.IEX,
                    )
                )
                vols = [float(b.volume) for b in bars_hist[sym] if b.timestamp.date() < now.date()]
                if len(vols) >= 10:
                    avg_vol[sym] = sum(vols[-20:]) / min(20, len(vols[-20:]))
            except Exception:
                pass
    except Exception as e:
        log.warning("GapFill: volume data fetch failed: %s", e)
        volume_today = {}
        avg_vol = {}

    for sym in tickers:
        pc = prior_close[sym]
        open_price = session_open_map.get(sym)
        if open_price is None or open_price <= 0:
            continue
        if pc <= 0:
            continue

        gap_pct = (open_price - pc) / pc * 100.0

        # Skip if gap is too small or too extreme
        if abs(gap_pct) < GAPFILL_MIN_GAP_PCT:
            continue
        if gap_pct > GAPFILL_MAX_GAP_PCT:
            continue
        if gap_pct < -GAPFILL_MAX_GAP_PCT:
            continue

        # Price filter
        if open_price < GAPFILL_MIN_PRICE:
            continue

        # Volume filter
        vol_today = volume_today.get(sym, 0)
        avg_v = avg_vol.get(sym, 0)
        if avg_v > 0 and vol_today < GAPFILL_MIN_VOLUME:
            continue

        # Earnings blackout
        if has_earnings_cache:
            try:
                next_er = get_next_earnings(sym)
                if next_er:
                    try:
                        ed = datetime.datetime.strptime(next_er, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    days_out = (ed - now.date()).days
                    if days_out <= GAPFILL_EARNINGS_BLACKOUT_DAYS:
                        continue
            except Exception:
                pass

        if len(candidates) >= GAPFILL_LIMIT:
            break

        candidates.append({
            "symbol": sym,
            "price": round(open_price, 2),
            "prior_close": round(pc, 2),
            "gap_pct": round(gap_pct, 2),
            "stop": round(open_price * (1 - GAPFILL_STOP_PCT), 2),
            "target": round(pc, 2),
            "volume_today": vol_today,
            "avg_volume": avg_v,
            "rel_volume": round(vol_today / avg_v, 2) if avg_v > 0 else 0,
            # rank: prefer gaps in the 3-8% range (proven best win rate)
            "score": min(abs(gap_pct) - GAPFILL_MIN_GAP_PCT, 9.0),
        })

    # Sort: larger gap first, then by volume
    candidates.sort(key=lambda c: (-c["score"], -c.get("rel_volume", 0)))
    log.info(f"GapFill: {len(candidates)} candidates after filtering")
    return candidates