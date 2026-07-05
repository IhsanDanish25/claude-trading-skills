"""Point-in-time historical signal generators for the satellite strategies.

Mirrors core.breakout_screener / core.meanrev_screener math exactly (same
filters, same lookback windows) but walks each symbol's cached bar history
day-by-day instead of calling FMP live, so it can run entirely offline from
backtest_harness/cache/*.json.

Each signal day D means "the live screener would have surfaced this symbol
using data through D's close" — consumed the same way earnings_data.py's
{symbol, date, surprise_pct} rows are: earnings_engine.run_earnings_simulation
enters at the next trading day's open after D, so there is no look-ahead.
"""
from __future__ import annotations

import datetime
import math

from core.config import (
    BREAKOUT_MIN_AVG_VOLUME,
    BREAKOUT_MIN_PRICE,
    BREAKOUT_VOL_MULT,
    EARNMOM_MAX_DAYS_AGO,
    EARNMOM_MIN_AVG_VOLUME,
    EARNMOM_MIN_DRIFT_PCT,
    EARNMOM_MIN_PRICE,
    EARNMOM_MIN_SURPRISE_PCT,
    MEANREV_BB_THRESHOLD,
    MEANREV_MIN_AVG_VOLUME,
    MEANREV_MIN_PRICE,
    MEANREV_RSI_THRESHOLD,
)

_EARNMOM_MIN_AGE = 8   # drift phase starts day 8 (gap-fill done, thesis confirmed)

_RSI_PERIOD = 14
_SMA200_PERIOD = 200
_BB_PERIOD = 20
_BB_STD = 2.0


def _avg(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _stddev(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    mean = sum(values[-n:]) / n
    variance = sum((v - mean) ** 2 for v in values[-n:]) / n
    return math.sqrt(variance)


def _rsi(closes: list[float], period: int = _RSI_PERIOD) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr14(bars: list[dict], n: int = 14) -> float:
    if len(bars) < n + 1:
        return 0.0
    window = bars[-(n + 1):]
    trs = []
    for prev, cur in zip(window[:-1], window[1:]):
        trs.append(max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        ))
    return sum(trs) / len(trs) if trs else 0.0


def _clearance_score(price: float, high_50: float) -> float:
    if high_50 <= 0:
        return 0.0
    clearance = (price - high_50) / high_50 * 100.0
    return min(40.0, max(0.0, clearance * 4.0))


def _volume_score(current_vol: float, avg_vol: float, mult: float) -> float:
    if avg_vol <= 0 or current_vol <= 0:
        return 0.0
    ratio = current_vol / avg_vol
    if ratio < mult:
        return 0.0
    return min(30.0, (ratio - mult) * 15.0 + 10.0)


def _compression_score(atr_pct: float) -> float:
    if atr_pct < 0:
        return 0.0
    if atr_pct <= 2.0:
        return 20.0
    if atr_pct <= 4.0:
        return 10.0
    return 0.0


def get_historical_breakout_signals(
    store, symbols: list[str], start_date, end_date,
) -> list[dict]:
    """Replica of core.breakout_screener.screen(), walked day-by-day.

    Filters (identical to live): price>=BREAKOUT_MIN_PRICE, 20d avg
    volume>=BREAKOUT_MIN_AVG_VOLUME, price>=SMA50, price at/above the 50-day
    high (or within 1% early-breakout tolerance), volume>=BREAKOUT_VOL_MULT x
    20d average. Score = clearance + volume-surge + ATR-compression points
    (same weighting as the live scorer), used only to rank same-day
    competitors for the sim's limited daily buy slots.

    Returns rows sorted by (date, -score): {symbol, date, surprise_pct}
    (field name kept for drop-in reuse with earnings_engine.run_earnings_simulation).
    """
    start = start_date if isinstance(start_date, datetime.date) else datetime.date.fromisoformat(start_date)
    end = end_date if isinstance(end_date, datetime.date) else datetime.date.fromisoformat(end_date)
    out: list[dict] = []

    for sym in symbols:
        bars = store.series.get(sym, [])
        if len(bars) < 55:
            continue
        closes = [b["close"] for b in bars]
        volumes = [b["volume"] for b in bars]

        for i in range(54, len(bars)):
            d = datetime.date.fromisoformat(bars[i]["date"])
            if d < start or d > end:
                continue
            price = closes[i]
            if price < BREAKOUT_MIN_PRICE:
                continue
            vol_window = volumes[max(0, i - 19):i + 1]
            avg_vol = _avg(vol_window)
            if avg_vol < BREAKOUT_MIN_AVG_VOLUME:
                continue
            high_50 = max(b["high"] for b in bars[i - 49:i + 1])
            if high_50 <= 0:
                continue
            clearance = (price - high_50) / high_50 * 100.0
            if price <= high_50 and clearance < -1.0:
                continue  # not yet broken out (beyond 1% early-breakout tolerance)
            sma50 = _sma(closes[:i + 1], 50)
            if sma50 is None or price < sma50:
                continue  # below SMA50 = reversal context, not a breakout
            current_vol = volumes[i]
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0.0
            if vol_ratio < BREAKOUT_VOL_MULT:
                continue
            atr = _atr14(bars[max(0, i - 14):i + 1])
            atr_pct = (atr / price * 100.0) if price > 0 else 0.0
            score = (_clearance_score(price, high_50)
                     + _volume_score(current_vol, avg_vol, BREAKOUT_VOL_MULT)
                     + _compression_score(atr_pct))
            out.append({
                "symbol": sym,
                "date": bars[i]["date"],
                "surprise_pct": round(score, 2),
                "clearance_pct": round(clearance, 2),
                "volume_ratio": round(vol_ratio, 2),
            })

    out.sort(key=lambda r: (r["date"], -r["surprise_pct"]))
    return out


def _earnmom_score(drift_pct: float, surprise_pct: float) -> float:
    """Same weighting as core.earnings_momentum_screener._drift_score:
    up to 60 pts for drift (10% = 60), up to 40 pts for surprise (20% = 40)."""
    drift_pts = min(60.0, max(0.0, drift_pct * 6.0))
    surprise_pts = min(40.0, max(0.0, surprise_pct * 2.0))
    return drift_pts + surprise_pts


def get_historical_earnmom_signals(
    store, symbols: list[str], start_date, end_date,
) -> list[dict]:
    """Replica of core.earnings_momentum_screener.screen(), walked day-by-day.

    Earnings-momentum "drift" trade: a stock that BEAT (surprise >=
    EARNMOM_MIN_SURPRISE_PCT) 8-EARNMOM_MAX_DAYS_AGO days ago and has drifted up
    >= EARNMOM_MIN_DRIFT_PCT since the beat. Emits one signal per beat, on the
    first day in the 8-45d window that clears price / volume / drift — the entry
    the live screener would have surfaced.

    Earnings dates + surprise % come from backtest_harness/earnings_data.py
    (yfinance, disk-cached); drift / price / volume come from the OHLCV store.
    No look-ahead: drift on day D uses only bars through D; earnings_engine
    enters at D+1 open.

    Returns rows sorted by (date, -score): {symbol, date, surprise_pct, ...}
    (surprise_pct carries the earnmom score for the sim's ranking, matching the
    breakout/meanrev generators).
    """
    from backtest_harness import earnings_data

    start = start_date if isinstance(start_date, datetime.date) else datetime.date.fromisoformat(start_date)
    end = end_date if isinstance(end_date, datetime.date) else datetime.date.fromisoformat(end_date)
    out: list[dict] = []

    for sym in symbols:
        bars = store.series.get(sym, [])
        if len(bars) < 21:
            continue
        dates = [b["date"] for b in bars]
        closes = [b["close"] for b in bars]
        volumes = [b["volume"] for b in bars]

        earnings = earnings_data.get_symbol_earnings(sym)
        for e in earnings:
            sp = e.get("surprise_pct")
            bd = (e.get("date") or "")[:10]
            if sp is None or not bd or sp < EARNMOM_MIN_SURPRISE_PCT:
                continue
            beat = datetime.date.fromisoformat(bd)

            # Beat reference bar = first bar on/after the report date.
            bi = next((i for i, dd in enumerate(dates) if dd >= bd), None)
            if bi is None:
                continue
            beat_close = closes[bi]
            if beat_close <= 0:
                continue

            for i in range(bi, len(bars)):
                d = datetime.date.fromisoformat(dates[i])
                age = (d - beat).days
                if age < _EARNMOM_MIN_AGE:
                    continue
                if age > EARNMOM_MAX_DAYS_AGO:
                    break
                if d < start or d > end:
                    continue
                price = closes[i]
                if price < EARNMOM_MIN_PRICE:
                    continue
                avg_vol = _avg(volumes[max(0, i - 19):i + 1])
                if avg_vol < EARNMOM_MIN_AVG_VOLUME:
                    continue
                drift = (price - beat_close) / beat_close * 100.0
                if drift < EARNMOM_MIN_DRIFT_PCT:
                    continue
                out.append({
                    "symbol": sym,
                    "date": dates[i],
                    "surprise_pct": round(_earnmom_score(drift, sp), 2),
                    "drift_pct": round(drift, 2),
                    "eps_surprise_pct": round(float(sp), 2),
                    "age_days": age,
                })
                break  # one entry per beat (held-set would block re-entry anyway)

    out.sort(key=lambda r: (r["date"], -r["surprise_pct"]))
    return out


def get_historical_meanrev_signals(
    store, symbols: list[str], start_date, end_date,
) -> list[dict]:
    """Replica of core.meanrev_screener.screen(), walked day-by-day.

    Filters (identical to live): price>=MEANREV_MIN_PRICE, 20d avg
    volume>=MEANREV_MIN_AVG_VOLUME, price>SMA200, RSI(14)<MEANREV_RSI_THRESHOLD,
    price<=lower Bollinger Band(20,2)+MEANREV_BB_THRESHOLD. Score =
    MEANREV_RSI_THRESHOLD - RSI (lower RSI = higher score = more oversold),
    same ranking rule as the live scorer.

    Returns rows sorted by (date, -score): {symbol, date, surprise_pct}
    (field name kept for drop-in reuse with earnings_engine.run_earnings_simulation).
    """
    start = start_date if isinstance(start_date, datetime.date) else datetime.date.fromisoformat(start_date)
    end = end_date if isinstance(end_date, datetime.date) else datetime.date.fromisoformat(end_date)
    out: list[dict] = []

    for sym in symbols:
        bars = store.series.get(sym, [])
        if len(bars) < _SMA200_PERIOD + 1:
            continue
        closes = [b["close"] for b in bars]
        volumes = [b["volume"] for b in bars]

        for i in range(_SMA200_PERIOD, len(bars)):
            d = datetime.date.fromisoformat(bars[i]["date"])
            if d < start or d > end:
                continue
            price = closes[i]
            if price < MEANREV_MIN_PRICE:
                continue
            vol_window = volumes[max(0, i - 19):i + 1]
            avg_vol = _avg(vol_window)
            if avg_vol < MEANREV_MIN_AVG_VOLUME:
                continue
            window_closes = closes[:i + 1]
            sma200 = _sma(window_closes, _SMA200_PERIOD)
            if sma200 is None or price <= sma200:
                continue
            rsi = _rsi(window_closes)
            if rsi is None or rsi >= MEANREV_RSI_THRESHOLD:
                continue
            bb_sma = _sma(window_closes, _BB_PERIOD)
            bb_sd = _stddev(window_closes, _BB_PERIOD)
            if bb_sma is None or bb_sd is None:
                continue
            bb_lower = bb_sma - _BB_STD * bb_sd
            if price > bb_lower + MEANREV_BB_THRESHOLD:
                continue
            score = max(0.0, MEANREV_RSI_THRESHOLD - rsi)
            out.append({
                "symbol": sym,
                "date": bars[i]["date"],
                "surprise_pct": round(score, 2),
                "rsi": round(rsi, 1),
                "bb_lower": round(bb_lower, 2),
            })

    out.sort(key=lambda r: (r["date"], -r["surprise_pct"]))
    return out
