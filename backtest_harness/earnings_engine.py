"""Earnings-momentum day-by-day simulation (Scenario E).

A separate sim loop from engine.py — engine.py drives the VCP composite strategy
(scenarios A/B/C/D) and is left untouched. This one implements a distinct edge:

  Entry : buy at the OPEN of the first trading day AFTER an EPS surprise >= 10%.
          (No look-ahead: the report date is known by the next open; the
          liquidity filter and ATR are computed from bars dated <= as_of=T-1.)
  Exit  : whichever comes first —
            * ATR(14) x mult TRAILING stop (ratchets up on close, never down), or
            * a hard time stop after `hold_days` calendar days.
          No profit target.
  Gate  : when regime_gated, only open new positions while regime_gate.classify()
          on SPY (bars <= as_of) returns GO/NEUTRAL; STAND_DOWN holds existing
          positions but blocks new entries.

Sizing/caps reuse the live config (MAX_POSITION_SIZE_PCT, MAX_OPEN_POSITIONS).
Lot/Portfolio/_atr14 are reused from engine.py so trade records stay
schema-compatible with metrics.py and validation_gates.
"""
from __future__ import annotations

import bisect
import datetime
import logging

from backtest_harness import data
from backtest_harness.engine import Lot, Portfolio, _atr14
from core import config

log = logging.getLogger("backtest.earnings_engine")


def _bars_asof(store: "data.BarStore", symbol: str, as_of: datetime.date) -> list[dict]:
    iso = as_of.isoformat()
    return [b for b in store.series.get(symbol, []) if b["date"] <= iso]


def _price_asof(store: "data.BarStore", symbol: str, as_of: datetime.date) -> float:
    bars = _bars_asof(store, symbol, as_of)
    return bars[-1]["close"] if bars else 0.0


def _avg_vol_asof(store: "data.BarStore", symbol: str, as_of: datetime.date, n: int = 20) -> float:
    bars = _bars_asof(store, symbol, as_of)
    vols = [b["volume"] for b in bars[-n:] if b.get("volume")]
    return sum(vols) / len(vols) if vols else 0.0


def _initial_stop(basis: float, atr: float | None, atr_stop_mult: float) -> float:
    """ATR stop below the fill; falls back to flat STOP_LOSS_PCT if ATR is
    unavailable or would push the stop <= 0."""
    if atr is not None:
        stop = basis - atr_stop_mult * atr
        if stop > 0:
            return round(stop, 2)
    return round(basis * (1 - config.STOP_LOSS_PCT), 2)


def run_earnings_simulation(
    store: "data.BarStore",
    surprises: list[dict],
    start_equity: float = 100_000.0,
    warmup: int = 70,
    slippage_bps: float = 10.0,
    atr_stop_mult: float = 1.5,
    hold_days: int = 60,
    regime_gated: bool = True,
    window_start: str | None = None,
    window_end: str | None = None,
    min_surprise_pct: float = 10.0,
    min_price: float = 10.0,
    min_avg_volume: float = 500_000.0,
    trailing_stop: bool = True,
    fixed_stop_pct: float | None = None,
) -> Portfolio:
    cal = store.trading_calendar("SPY")
    if len(cal) <= warmup + 1:
        raise RuntimeError(f"Not enough SPY history ({len(cal)} bars) for warmup={warmup}")

    ws = datetime.date.fromisoformat(window_start) if window_start else cal[0]
    we = datetime.date.fromisoformat(window_end) if window_end else cal[-1]

    # Map each qualifying surprise to its entry day = first trading day strictly
    # after the report date. Highest-surprise names get first claim on slots.
    entries_by_day: dict[datetime.date, list[dict]] = {}
    for s in surprises:
        if s.get("surprise_pct") is None or s["surprise_pct"] < min_surprise_pct:
            continue
        d = datetime.date.fromisoformat(s["date"])
        idx = bisect.bisect_right(cal, d)
        if idx >= len(cal):
            continue
        entries_by_day.setdefault(cal[idx], []).append(s)
    for d in entries_by_day:
        entries_by_day[d].sort(key=lambda x: x["surprise_pct"], reverse=True)

    pf = Portfolio(cash=start_equity)
    last_px: dict[str, float] = {}
    slip = slippage_bps / 10_000.0
    buy_fill = lambda px: px * (1 + slip)    # noqa: E731
    sell_fill = lambda px: px * (1 - slip)   # noqa: E731
    MAXP, MAX_OPEN = config.MAX_POSITION_SIZE_PCT, config.MAX_OPEN_POSITIONS

    start_i = max(warmup, bisect.bisect_left(cal, ws))
    for i in range(start_i, len(cal)):
        T = cal[i]
        if T > we:
            break
        as_of = cal[i - 1]
        data.set_as_of(as_of)

        # refresh last-known prices (prior close) for anything we may mark
        for sym in pf.held_symbols() | {s["symbol"] for s in entries_by_day.get(T, [])} | {"SPY"}:
            b = store.bar_on(sym, as_of)
            if b:
                last_px[sym] = b["close"]

        def _open_px(sym: str) -> float:
            b = store.bar_on(sym, T)
            return b["open"] if b else last_px.get(sym, 0.0)

        # ── regime gate (decided from as_of=T-1, no look-ahead) ───────────────
        allow_entries = True
        if regime_gated:
            spy_bars = _bars_asof(store, "SPY", as_of)
            if len(spy_bars) >= 50:
                from regime_gate import classify as _regime_classify
                _reg = _regime_classify(
                    [b["high"] for b in spy_bars],
                    [b["low"] for b in spy_bars],
                    [b["close"] for b in spy_bars],
                )
                allow_entries = _reg.can_trade

        # ── ENTRIES: day-after-earnings, filled at T open ─────────────────────
        if allow_entries and entries_by_day.get(T):
            pv_open = pf.cash + sum(l.qty * _open_px(l.symbol) for l in pf.lots)
            held = pf.held_symbols()
            for s in entries_by_day[T]:
                if len(pf.lots) >= MAX_OPEN:
                    break
                sym = s["symbol"]
                if sym in held:
                    continue
                op = _open_px(sym)
                if op <= 0:
                    continue  # no tradable open on T
                # liquidity filter, point-in-time (<= as_of)
                if _price_asof(store, sym, as_of) <= min_price:
                    continue
                if _avg_vol_asof(store, sym, as_of) <= min_avg_volume:
                    continue
                qty = int((pv_open * MAXP) / op)
                if qty < 1:
                    continue
                basis = buy_fill(op)
                if pf.cash < qty * basis:
                    continue
                if fixed_stop_pct is not None:
                    stop = round(basis * (1 - fixed_stop_pct), 2)
                else:
                    stop = _initial_stop(basis, _atr14(store, sym, as_of), atr_stop_mult)
                pf.lots.append(Lot(sym, T, basis, qty, stop, float("inf")))
                pf.cash -= qty * basis
                held.add(sym)

        # ── EXITS: trailing-stop touch first, then time stop ──────────────────
        survivors: list[Lot] = []
        for l in pf.lots:
            bar = store.bar_on(l.symbol, T)
            if not bar:
                survivors.append(l)
                continue
            level = exit_reason = None
            if bar["low"] <= l.stop:
                level, exit_reason = l.stop, "stop"
            elif (T - l.entry_date).days >= hold_days:
                level, exit_reason = bar["close"], "time"
            if exit_reason is None:
                survivors.append(l)
                continue
            exit_price = sell_fill(level)
            pf.cash += l.qty * exit_price
            pf.trades.append({
                "symbol": l.symbol,
                "entry_date": l.entry_date.isoformat(),
                "exit_date": T.isoformat(),
                "entry_price": round(l.entry_price, 4),
                "exit_price": round(exit_price, 4),
                "qty": l.qty,
                "return_pct": round((exit_price / l.entry_price - 1) * 100, 4),
                "pnl_usd": round((exit_price - l.entry_price) * l.qty, 2),
                "holding_days": (T - l.entry_date).days,
                "exit_reason": exit_reason,
                "is_pyramid": False,
            })
        pf.lots = survivors

        # ── ratchet the ATR trailing stop up on the T close (never down) ──────
        if trailing_stop:
            for l in pf.lots:
                bar = store.bar_on(l.symbol, T)
                if not bar:
                    continue
                atr = _atr14(store, l.symbol, T)
                if atr is not None:
                    cand = round(bar["close"] - atr_stop_mult * atr, 2)
                    if cand > l.stop:
                        l.stop = cand

        # ── mark equity at T close ────────────────────────────────────────────
        eq = pf.cash
        for l in pf.lots:
            bar = store.bar_on(l.symbol, T)
            px = bar["close"] if bar else last_px.get(l.symbol, l.entry_price)
            eq += l.qty * px
        pf.equity_curve.append({"date": T.isoformat(), "equity": round(eq, 2)})

    return pf
