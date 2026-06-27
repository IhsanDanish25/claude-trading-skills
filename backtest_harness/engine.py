"""Day-by-day simulation that drives the REAL strategy functions.

Fidelity contract:
  * Entries: exact replica of routines/market_open.py's decision block —
    core.screener.screen() + the price/RS/gap/score filters + composite ranking
    via core.composite.build_context()/compute_composite(). Top
    min(MAX_BUYS=3, MAX_OPEN_POSITIONS - open) names are bought at 5% of PV.
  * Exits: the OCO bracket attached at entry — stop -STOP_LOSS_PCT / target
    +TAKE_PROFIT_PCT, full size. Same-day touch of both => stop-first (conservative).
  * Management: the MECHANICAL parts of routines/midday_review.py — 4% trailing
    ratchet (core.edge.compute_trail_stop) and one pyramid add at +3%
    (core.edge.should_pyramid), sized at 2.5% of PV with its own bracket.
  * EXCLUDED (documented): Claude SELL/TIGHTEN position review and the LLM midday
    scan (non-deterministic, not the composite strategy); regime trade_bias gating
    and the partial-profit branch (shadowed by the same-level +6% OCO target),
    and FMP earnings/fundamental sub-scores (disabled to avoid look-ahead).

No look-ahead: every decision for trading day T is computed from data through
T-1 (AS_OF) and filled at T's open.
"""
from __future__ import annotations

import os
# Disable FMP GROUP B sub-scores BEFORE composite imports (USE_FMP is read at import).
os.environ.setdefault("COMPOSITE_USE_FMP", "false")

import datetime
import logging
from dataclasses import dataclass, field

import core.screener as screener
from core import config, composite
from core.edge import compute_trail_stop, should_pyramid

from backtest_harness import data

log = logging.getLogger("backtest.engine")

# Monkeypatch the live fetcher with the point-in-time slicer. build_context()
# does `from core.screener import _fetch_bars` at call time and screen() uses the
# module global, so this single patch covers both.
screener._fetch_bars = data.fake_fetch

MAX_BUYS = 3  # routines/market_open.py hardcodes this per-run cap


def _atr14(store: "data.BarStore", symbol: str, as_of: datetime.date, n: int = 14) -> float | None:
    """ATR(14) from daily bars dated <= as_of (point-in-time). Simple average of
    the last `n` true ranges. Returns None if fewer than n+1 bars available."""
    bars = [b for b in store.series.get(symbol, [])
            if datetime.date.fromisoformat(b["date"]) <= as_of]
    if len(bars) < n + 1:
        return None
    window = bars[-(n + 1):]
    trs = []
    for prev, cur in zip(window[:-1], window[1:]):
        tr = max(cur["high"] - cur["low"],
                 abs(cur["high"] - prev["close"]),
                 abs(cur["low"] - prev["close"]))
        trs.append(tr)
    atr = sum(trs) / len(trs)
    return atr if atr > 0 else None


def _initial_stop(basis: float, store: "data.BarStore", symbol: str, as_of: datetime.date,
                  stop_mode: str, atr_stop_mult: float) -> float:
    """Initial protective stop. 'flat' = basis*(1-STOP_LOSS_PCT) (live behavior);
    'atr' = basis - atr_stop_mult*ATR(14). Falls back to flat if ATR is
    unavailable or would push the stop <= 0."""
    if stop_mode == "atr":
        atr = _atr14(store, symbol, as_of)
        if atr is not None:
            stop = basis - atr_stop_mult * atr
            if stop > 0:
                return round(stop, 2)
    return round(basis * (1 - config.STOP_LOSS_PCT), 2)


@dataclass
class Lot:
    symbol: str
    entry_date: datetime.date
    entry_price: float
    qty: int
    stop: float
    target: float
    is_pyramid: bool = False
    pyramided: bool = False  # primary lot only: has it spawned its pyramid add?


@dataclass
class Portfolio:
    cash: float
    lots: list[Lot] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)

    def held_symbols(self) -> set[str]:
        return {l.symbol for l in self.lots}


def _select_ranked_candidates(universe: list[str], held: set[str], bought_today: set[str]) -> list[dict]:
    """Exact replica of market_open.py's filter + composite-ranking block."""
    candidates = screener.screen(universe)
    passed = []
    for c in candidates:
        sym = c["symbol"]
        price = c.get("price", 0)
        rs = c.get("rs_vs_spy")
        gap = c.get("gap_pct", 0)
        score = c.get("score", 0)
        if sym in held or sym in bought_today:
            continue
        if not (config.MIN_PRICE <= price <= config.MAX_PRICE):
            continue
        if rs is not None and rs < config.MIN_RS_RATING:
            continue
        if abs(gap) > config.MAX_GAP_PCT:
            continue
        if score < config.MIN_COMPOSITE_SCORE:
            continue
        passed.append(c)
    if not passed:
        return []

    passed_syms = [c["symbol"] for c in passed]
    ctx = composite.build_context(extra_symbols=passed_syms)
    bars_map = data.fake_fetch(passed_syms, days=60)
    for c in passed:
        result = composite.compute_composite(c, bars_map.get(c["symbol"]), ctx)
        c["composite_final"] = result["final"]
    passed.sort(key=lambda x: x.get("composite_final", 0), reverse=True)
    return passed


def _equity(pf: Portfolio, store: data.BarStore, d: datetime.date, last_px: dict) -> float:
    val = pf.cash
    for l in pf.lots:
        bar = store.bar_on(l.symbol, d)
        px = bar["close"] if bar else last_px.get(l.symbol, l.entry_price)
        val += l.qty * px
    return val


def _open_px(store: data.BarStore, sym: str, d: datetime.date, last_px: dict) -> float:
    bar = store.bar_on(sym, d)
    return bar["open"] if bar else last_px.get(sym, 0.0)


def run_simulation(store: data.BarStore, universe: list[str], start_equity: float = 100_000.0,
                   warmup: int = 70, slippage_bps: float = 0.0, stop_mode: str = "flat",
                   atr_stop_mult: float = 1.5) -> Portfolio:
    cal = store.trading_calendar("SPY")
    if len(cal) <= warmup + 1:
        raise RuntimeError(f"Not enough SPY history ({len(cal)} bars) for warmup={warmup}")

    pf = Portfolio(cash=start_equity)
    last_px: dict[str, float] = {}
    STOP, TP = config.STOP_LOSS_PCT, config.TAKE_PROFIT_PCT
    MAXP, MAX_OPEN = config.MAX_POSITION_SIZE_PCT, config.MAX_OPEN_POSITIONS

    # Slippage moves every fill against us: buys fill higher, sells/exits lower.
    slip = slippage_bps / 10_000.0
    buy_fill = lambda px: px * (1 + slip)    # noqa: E731
    sell_fill = lambda px: px * (1 - slip)   # noqa: E731

    for i in range(warmup, len(cal)):
        T = cal[i]
        as_of = cal[i - 1]
        data.set_as_of(as_of)

        # refresh last-known prices from the prior close
        for s in universe + data.INDEX_SYMBOLS + data.SECTOR_ETFS:
            b = store.bar_on(s, as_of)
            if b:
                last_px[s] = b["close"]

        day_start_equity = _equity(pf, store, as_of, last_px)  # equity at T-1 close

        # ── a. portfolio value at T open + circuit-breaker gate ───────────────
        pv_open = pf.cash
        for l in pf.lots:
            pv_open += l.qty * _open_px(store, l.symbol, T, last_px)
        cb_blocked = (day_start_equity > 0 and
                      (pv_open - day_start_equity) / day_start_equity <= -config.CIRCUIT_BREAKER_PCT)

        # ── b. ENTRIES (decided from as_of, filled at T open) ─────────────────
        entered_today: set[str] = set()
        if not cb_blocked:
            held = pf.held_symbols()
            slots = min(MAX_BUYS, MAX_OPEN - len(held))
            if slots > 0:
                ranked = _select_ranked_candidates(universe, held, entered_today)
                for c in ranked:
                    if slots <= 0:
                        break
                    sym = c["symbol"]
                    op = _open_px(store, sym, T, last_px)
                    if op <= 0:
                        continue  # no tradable open on T
                    qty = int((pv_open * MAXP) / op)  # size on observable open
                    if qty < 1:
                        continue
                    basis = buy_fill(op)             # slipped fill price = cost basis
                    stop = _initial_stop(basis, store, sym, as_of, stop_mode, atr_stop_mult)
                    pf.lots.append(Lot(sym, T, basis, qty, stop, round(basis * (1 + TP), 2)))
                    pf.cash -= qty * basis
                    entered_today.add(sym)
                    slots -= 1

        # ── c. EXITS via OCO bracket (stop-first on same-day double touch) ─────
        survivors: list[Lot] = []
        for l in pf.lots:
            bar = store.bar_on(l.symbol, T)
            if not bar:
                survivors.append(l)
                continue
            level = exit_reason = None
            if bar["low"] <= l.stop:
                level, exit_reason = l.stop, "stop"
            elif bar["high"] >= l.target:
                level, exit_reason = l.target, "target"
            if level is None:
                survivors.append(l)
                continue
            exit_price = sell_fill(level)  # exits fill against us by slippage
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
                "is_pyramid": l.is_pyramid,
            })
        pf.lots = survivors

        # ── d. management at T close: trailing ratchet + one pyramid add ──────
        pv_close = _equity(pf, store, T, last_px)
        for l in list(pf.lots):
            bar = store.bar_on(l.symbol, T)
            if not bar:
                continue
            close = bar["close"]
            if close > l.entry_price:
                l.stop = compute_trail_stop(close, l.entry_price, l.stop)  # real edge fn
            if (not l.is_pyramid and
                    should_pyramid({"pnl_pct": (close / l.entry_price - 1) * 100,
                                    "pyramided": l.pyramided})):
                add_amt = pv_close * MAXP * 0.5
                add_qty = int(add_amt / close)        # size on observable close
                add_basis = buy_fill(close)           # slipped fill = cost basis
                if add_qty >= 1 and pf.cash >= add_qty * add_basis:
                    add_stop = _initial_stop(add_basis, store, l.symbol, T, stop_mode, atr_stop_mult)
                    pf.lots.append(Lot(l.symbol, T, add_basis, add_qty, add_stop,
                                       round(add_basis * (1 + TP), 2), is_pyramid=True))
                    pf.cash -= add_qty * add_basis
                    l.pyramided = True

        # ── e. mark equity at T close ─────────────────────────────────────────
        pf.equity_curve.append({"date": T.isoformat(),
                                "equity": round(_equity(pf, store, T, last_px), 2)})

    return pf
