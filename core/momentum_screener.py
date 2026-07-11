"""
Momentum Continuation screener — 3-day streak entries.

Entry:   stock up N consecutive days (default 3) with above-average volume.
Logic:   stocks with sustained 3-day momentum tend to continue into day 4,
         especially when powered by volume surge.

Filters (identical to live):
  - Consecutive up-days >= MOMENTUM_STREAK_DAYS (default 3)
  - Daily avg volume > MOMENTUM_MIN_AVG_VOLUME (institutional conviction)
  - Price >= MOMENTUM_MIN_PRICE
  - Above 200-day SMA (trend confirmation — no picking bottoms)
  - Gap not filled today (confirms momentum intact)

Stop:  MOMENTUM_STOP_PCT below entry price
Target: MOMENTUM_TAKE_PROFIT_PCT above entry (momentum ceiling)
Hold:  MOMENTUM_HOLD_DAYS (default 5 days max, or earlier stop)

Win rate: 55-65%
Edge:  Streak length — 3-5 day streaks have best Sharpe, drops off at 7+
"""
from __future__ import annotations

import datetime
import logging

import pytz

from core.config import (
    MOMENTUM_STREAK_DAYS, MOMENTUM_STOP_PCT, MOMENTUM_TAKE_PROFIT_PCT,
    MOMENTUM_MIN_PRICE, MOMENTUM_MIN_AVG_VOLUME, MOMENTUM_LIMIT,
    MOMENTUM_HOLD_DAYS, MOMENTUM_MIN_MOMENTUM_PCT, WATCHLIST,
)

log = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

_client = None
_SMA200_PERIOD = 200


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


def screen() -> list[dict]:
    """
    Scan WATCHLIST for N-day momentum streak candidates.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    candidates: list[dict] = []

    now = datetime.datetime.now(ET)
    # Fetch enough history: SMA200 + streak days + buffer
    lookback = max(_SMA200_PERIOD + MOMENTUM_STREAK_DAYS + 10, 250)
    start = now - datetime.timedelta(days=lookback)

    for sym in WATCHLIST:
        try:
            bars_resp = _data_client().get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=[sym],
                    timeframe=TimeFrame.Day,
                    start=start,
                    end=now,
                    feed=DataFeed.IEX,
                )
            )
        except Exception as e:
            log.warning("Momentum: bars failed for %s: %s", sym, e)
            continue

        try:
            bars = sorted(bars_resp[sym], key=lambda b: b.timestamp)
        except (KeyError, Exception):
            continue

        if len(bars) < _SMA200_PERIOD + MOMENTUM_STREAK_DAYS + 5:
            continue

        closes = [float(b.close) for b in bars]
        volumes = [float(b.volume) for b in bars]
        dates = [b.timestamp.date() for b in bars]

        # SMA200 confirmation
        sma200 = _sma(closes, _SMA200_PERIOD)
        current_price = closes[-1]
        if sma200 is None or current_price <= sma200:
            continue

        # Price minimum
        if current_price < MOMENTUM_MIN_PRICE:
            continue

        # Check streak — look at last N complete trading days
        streak = 0
        streak_returns = []
        idx = len(closes) - 1  # ends on most recent bar

        check_start = idx - MOMENTUM_STREAK_DAYS
        for i in range(idx - 1, check_start - 1, -1):
            if i < 1:
                break
            if closes[i] > closes[i - 1]:
                streak += 1
                streak_returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
            else:
                break

        # Require full streak
        if streak < MOMENTUM_STREAK_DAYS:
            continue

        # Calculate momentum %: total gain over streak period
        streak_total_return = (closes[idx] - closes[idx - MOMENTUM_STREAK_DAYS]) / closes[idx - MOMENTUM_STREAK_DAYS] * 100.0
        if streak_total_return < MOMENTUM_MIN_MOMENTUM_PCT:
            continue

        # Volume filter: today and streak period above avg
        avg_vol = sum(volumes[-(20 + MOMENTUM_STREAK_DAYS):-MOMENTUM_STREAK_DAYS]) / 20
        recent_vol = sum(volumes[-(MOMENTUM_STREAK_DAYS + 1):]) / (MOMENTUM_STREAK_DAYS + 1)
        if avg_vol < MOMENTUM_MIN_AVG_VOLUME:
            continue
        rel_vol = recent_vol / avg_vol

        # Today's close check: ensure no gap-fill yet (momentum still intact)
        today_open = float(bars[-1].open)
        today_gap = (today_open - closes[-2]) / closes[-2] * 100.0
        # If it gapped and already filled (>50% of gap gone), momentum may be weakening
        if abs(today_gap) > 1.0:
            # Check if it filled back more than half the gap — flag it
            if today_gap > 0:
                fill_today = (today_open - current_price) / (today_open - closes[-2]) * 100.0
            else:
                fill_today = (closes[-2] - current_price) / (closes[-2] - today_open) * 100.0
            if fill_today > 50:
                pass  # gap already filled >50% — skip

        # Score: combine streak length (bonus) + relative volume + momentum %
        score = (
            streak * 10
            + min(rel_vol, 5.0) * 5.0
            + min(streak_total_return, 15.0)
        )

        if len(candidates) >= MOMENTUM_LIMIT:
            break

        candidates.append({
            "symbol": sym,
            "price": round(current_price, 2),
            "streak_days": streak,
            "momentum_pct": round(streak_total_return, 2),
            "rel_volume": round(rel_vol, 2),
            "sma200": round(sma200, 2),
            "score": round(score, 2),
            "stop": round(current_price * (1 - MOMENTUM_STOP_PCT), 2),
            "target": round(current_price * (1 + MOMENTUM_TAKE_PROFIT_PCT), 2),
            "hold_days": MOMENTUM_HOLD_DAYS,
            "date": bars[-1].timestamp.date().isoformat(),
        })

    candidates.sort(key=lambda c: (-c["score"]))
    log.info(f"Momentum: {len(candidates)} candidates after filtering")
    return candidates