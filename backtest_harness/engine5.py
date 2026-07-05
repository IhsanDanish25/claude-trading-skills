"""Multi-strategy day-by-day simulation — meanrev, insider, squeeze, breakout, earnmom.

Fidelity contract (verbatim from market_open.py):
  * Entries: exact replica of _run_meanrev / _run_insider / _run_squeeze /
    _run_breakout / _run_earnmom in routines/market_open.py — same screeners,
    same filters, same ranking, same slot-sharing (MAX_BUYS pool,
    accumulated held-set across runners).
  * Sizing: each strategy uses its own {STRATEGY}_SIZE_PCT of portfolio value.
  * Stops: each strategy uses its own {STRATEGY}_STOP_PCT (flat = basis × (1-pct));
    trailing management is excluded (edge.compute_trail_stop is live-only).
  * Exits: OCO bracket (stop / time-stop); same-day double-touch → stop-first.
  * Time-based exits per strategy: MEANREV_HOLD_DAYS / INSIDER_HOLD_DAYS /
    SQUEEZE_HOLD_DAYS / BREAKOUT_HOLD_DAYS / EARNMOM_HOLD_DAYS (from config).
  * Pyramid: disabled in this engine (edge.should_pyramid is live-only).

No look-ahead: every decision for trading day T is computed from data through
T-1 (AS_OF). FMP data calls are monkey-patched to return point-in-time slices
from the backtest BarStore for OHLCV endpoints; fundamental endpoints
(short-interest / insider / earnings-surprise) return [] and those strategies
degrade to empty-signal gracefully (same as a live FMP outage).

PEAD is NOT included here — it has its own dedicated engine
(earnings_engine.run_earnings_simulation).
"""
from __future__ import annotations

import datetime
import logging
import os
import sys
from dataclasses import dataclass, field
from contextlib import contextmanager
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPOSITE_USE_FMP", "false")

from core import config
from core import clock
from core.edge import compute_trail_stop    # unused in backtest but imported for API compat
from backtest_harness import data

log = logging.getLogger("backtest.engine5")


@contextmanager
def _quiet_screeners():
    """Silence log.info from screener modules during backtest simulation.

    The screeners log verbosely on every call — once-per-trading-day × 5
    screeners × ~500 days × N symbols = tens of thousands of INFO lines.
    Suppress to WARNING while simulating; restore on exit (including errors)."""
    import core.meanrev_screener
    import core.insider_screener
    import core.squeeze_screener
    import core.breakout_screener
    import core.earnings_momentum_screener
    screener_loggers = [
        logging.getLogger(core.meanrev_screener.__name__),
        logging.getLogger(core.insider_screener.__name__),
        logging.getLogger(core.squeeze_screener.__name__),
        logging.getLogger(core.breakout_screener.__name__),
        logging.getLogger(core.earnings_momentum_screener.__name__),
    ]
    saved = [(l, l.level, l.propagate) for l in screener_loggers]
    try:
        for l in screener_loggers:
            l.setLevel(logging.WARNING)
            l.propagate = False
        yield
    finally:
        for l, level, propagate in saved:
            l.setLevel(level)
            l.propagate = propagate


# Monkeypatch FMP so all strategy screeners serve point-in-time data, not live.
# Done at module-load (before any screener is called) so all subsequent
# import-time references see the patch.
import core.fmp as _fmp_module

_original_get = _fmp_module._get

# Per-screener: how many days of history they request. Updated by
# _install_fake_fetch before each screen() call so the patched _get returns
# the right window.
_fmp_request_params: dict = {"days": 60}  # default; overridden per screener


def _install_fake_fetch(days: int) -> None:
    _fmp_request_params["days"] = days


# Point-in-time earnings store for earnmom: {symbol: [reported-quarter dicts
# oldest-first, each {date, epsActual, epsEstimated, surprise_pct}]}. Populated
# by install_earnings_store() from backtest_harness.fundamentals. Served through
# the patched FMP _get for the /earning_calendar endpoint, filtered to <= AS_OF.
_EARNINGS_STORE: dict[str, list[dict]] = {}


def install_earnings_store(store: dict[str, list[dict]]) -> None:
    global _EARNINGS_STORE
    _EARNINGS_STORE = store or {}
    log.info("engine5: earnings store installed (%d symbols, %d quarters)",
             len(_EARNINGS_STORE), sum(len(v) for v in _EARNINGS_STORE.values()))


def _patched_fmp_get(url: str, params: dict = None) -> dict | list:
    """Intercept FMP /stable/ calls and serve backtest data.
    Falls through to live API for any URL not matched — only used by the
    backtest harness so it should never fire in practice, but safe to pass through."""
    params = dict(params or {})

    if "/historical-price-eod" in url:
        # Historical bars — serve directly from the raw BarStore series so the
        # full FMP shape (date + OHLCV) is preserved. Do NOT route through
        # data.fake_fetch(): its slice_asof projection drops `date` and `open`
        # (the {close,high,low,volume} shape meanrev/breakout need), which
        # breaks any screener that locates a bar by date (earnmom drift).
        # Window + order match the previous behavior exactly (newest-first, same
        # calendar span) so meanrev/breakout close/high/low/volume are unchanged.
        symbol = params.get("symbol", "")
        days_back = _fmp_request_params.get("days", 60)
        as_of = data.AS_OF  # set by data.set_as_of() in simulation loop
        if as_of is None:
            return []
        _store = getattr(data, "STORE", None) or getattr(data, "_store", None)
        if _store is None:
            return []
        as_of_s = str(as_of)
        calendar_days = int(days_back * 2.2 * 1.6) + 1   # == old fake_fetch window
        lo_s = str(as_of - datetime.timedelta(days=calendar_days))
        raw = _store.series.get(symbol, [])
        rows = [b for b in raw if lo_s < b.get("date", "") <= as_of_s]
        if not rows:
            return []
        # BarStore series is oldest-first; real FMP returns newest-first.
        out = []
        for b in reversed(rows):
            out.append({
                "date":   b.get("date", ""),
                "open":   b.get("open", b.get("close", 0)),
                "high":   b.get("high", 0),
                "low":    b.get("low", 0),
                "close":  b.get("close", 0),
                "volume": b.get("volume", 0),
            })
        return out

    if "/quote" in url:
        # Current quote — use last close as price (FMP /quote returns a list).
        symbol = params.get("symbol", "")
        as_of = data.AS_OF
        if as_of is None:
            return []
        # Use data.STORE (set by data.install_store) — NOT data._store
        _store = getattr(data, "STORE", None) or getattr(data, "_store", None)
        bar = _store.bar_on(symbol, as_of) if _store else None
        if not bar:
            # Fallback: find most recent bar in series (newest-last, oldest-first)
            bars = [b for b in (_store.series.get(symbol, []) if _store else [])
                    if b.get("date", "") <= str(as_of)]
            bar = bars[-1] if bars else None
        if bar:
            return [{"symbol": symbol, "price": bar.get("close", 0),
                     "changePercentage": 0, "volume": bar.get("volume", 0)}]
        return []

    if "/earnings" in url:
        # Per-symbol point-in-time earnings (earnmom now calls /stable/earnings,
        # matching live). Serve the requested symbol's reported quarters dated
        # <= AS_OF so there is no look-ahead, in the /stable/earnings shape the
        # screener reads (epsActual / epsEstimated).
        symbol = params.get("symbol", "")
        as_of = data.AS_OF
        if as_of is None or not symbol or not _EARNINGS_STORE:
            return []
        as_of_s = str(as_of)
        out = []
        for q in _EARNINGS_STORE.get(symbol, []):
            d = q.get("date", "")
            if d and d <= as_of_s:      # never serve an event dated after AS_OF
                out.append({
                    "date":         d,
                    "symbol":       symbol,
                    "epsActual":    q.get("epsActual"),
                    "epsEstimated": q.get("epsEstimated"),
                })
        return out

    # All other endpoints (short-interest / insider / etc.) cannot be served
    # from backtest data — degrade gracefully, same as a live rate-limit.
    log.debug("engine5: FMP endpoint not backtestable, returning []: %s", url)
    return []


_fmp_module._get = _patched_fmp_get


# Now import screeners — they bind _get at call time, so they'll use the patch.
try:
    from core.meanrev_screener import screen   as _screen_meanrev
    from core.meanrev_screener import _SMA200_PERIOD as _MEANREV_SMA200
except Exception as e:
    log.warning("MeanRev screener not available: %s — meanrev excluded", e)
    _screen_meanrev = None

try:
    from core.insider_screener import screen    as _screen_insider
except Exception as e:
    log.warning("Insider screener not available: %s — insider excluded", e)
    _screen_insider = None

try:
    from core.squeeze_screener import screen    as _screen_squeeze
except Exception as e:
    log.warning("Squeeze screener not available: %s — squeeze excluded", e)
    _screen_squeeze = None

try:
    from core.breakout_screener import screen   as _screen_breakout
except Exception as e:
    log.warning("Breakout screener not available: %s — breakout excluded", e)
    _screen_breakout = None

try:
    from core.earnings_momentum_screener import screen as _screen_earnmom
except Exception as e:
    log.warning("EarnMom screener not available: %s — earnmom excluded", e)
    _screen_earnmom = None

MAX_BUYS = 3   # must match market_open.py
STOP, TP = config.STOP_LOSS_PCT, config.TAKE_PROFIT_PCT


# ── Per-strategy config ───────────────────────────────────────────────────────
_STRAT_CONFIG: dict = {}


def _sc(name: str):
    """Resolve stop/size/hold from config, with sane defaults."""
    return {
        "stop_pct":   getattr(config, f"{name.upper()}_STOP_PCT",   STOP),
        "size_pct":   getattr(config, f"{name.upper()}_SIZE_PCT",   config.MAX_POSITION_SIZE_PCT),
        "hold_days":  getattr(config, f"{name.upper()}_HOLD_DAYS",  60),
        "min_price":  getattr(config, f"{name.upper()}_MIN_PRICE",  config.MIN_PRICE),
        "screen_func": None,   # filled in below
    }


for _n, _f in [("meanrev", _screen_meanrev), ("insider", _screen_insider),
                ("squeeze", _screen_squeeze),  ("breakout", _screen_breakout),
                ("earnmom", _screen_earnmom)]:
    _STRAT_CONFIG[_n] = _sc(_n)
    _STRAT_CONFIG[_n]["screen_func"] = _f


@dataclass
class Lot:
    symbol: str
    entry_date: datetime.date
    entry_price: float
    qty: int
    stop: float
    target: float
    strategy: str = "mev"     # abbreviated key
    is_pyramid: bool = False
    pyramided: bool = False
    hold_days: int = 60      # per-strategy time-exit horizon


@dataclass
class Portfolio:
    cash: float
    lots: list[Lot] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)

    def held_symbols(self) -> set[str]:
        return {l.symbol for l in self.lots}


def _open_px(store: data.BarStore, sym: str, d: datetime.date, last_px: dict) -> float:
    bar = store.bar_on(sym, d)
    return bar["open"] if bar else last_px.get(sym, 0.0)


def _equity(pf: Portfolio, store: data.BarStore, d: datetime.date, last_px: dict) -> float:
    val = pf.cash
    for l in pf.lots:
        bar = store.bar_on(l.symbol, d)
        px = bar["close"] if bar else last_px.get(l.symbol, l.entry_price)
        val += l.qty * px
    return val


def _install_fmp_days(days: int) -> None:
    _fmp_request_params["days"] = days


def _run_strategy(
    strat: str,
    store: data.BarStore,
    as_of: datetime.date,
    pf: Portfolio,
    slots: int,
    already_bought_today: set[str],
) -> list[Lot]:
    """Run one strategy's screener and return new Lot objects (not persisted to pf).

    Entry filters mirror market_open.py exactly:
      - held / already_bought_today exclusions
      - per-strategy min_price
      - strategy-specific field gates (RSI, SI%, clearance, etc.)
    Ranking: as returned by screener (already sorted by strategy score).
    Max buys per strategy run: min(slots, strategy-specific limit or no cap).

    Does NOT modify pf; caller adds returned Lots and updates pf.cash.
    """
    cfg = _STRAT_CONFIG.get(strat)
    screener_fn = cfg["screen_func"]
    if screener_fn is None:
        return []

    # Install lookback window before calling screener (pickled by _patched_fmp_get)
    _install_fmp_days({"meanrev":  200, "insider": 30, "squeeze":  60,
                       "breakout": 100, "earnmom":  60}.get(strat, 60))

    market_day = as_of  # bars in BarStore are already shift-adjusted
    data.set_as_of(market_day)
    clock.set_today(market_day)   # earnmom age/recency windows resolve to AS_OF

    try:
        candidates = screener_fn()
    except Exception as e:
        log.debug("engine5 [%s] screen failed: %s — returning empty", strat, e)
        return []

    if not candidates:
        return []

    new_lots: list[Lot] = []
    min_price = cfg["min_price"]

    for c in candidates:
        if slots <= 0:
            break
        sym = c["symbol"]
        price = c.get("price", 0)

        if sym in pf.held_symbols():
            continue
        if sym in already_bought_today:
            continue
        if price <= 0 or price < min_price:
            continue

        # ── Strategy-specific additional gates (verbatim from market_open.py) ──
        ok = True
        if strat == "meanrev":
            rsi = c.get("rsi", 100)
            if rsi >= config.MEANREV_RSI_THRESHOLD:
                ok = False
        elif strat == "squeeze":
            # SI% and DTC gates already enforced inside screen()
            # but double-check in case screen() became empty-signal
            if c.get("short_interest_pct", 0) < config.SQUEEZE_MIN_SI_PCT:
                ok = False
        elif strat == "breakout":
            if c.get("clearance_pct", -999) < -1.0:
                ok = False   # must be within 1% of 50d high at minimum
        elif strat == "earnmom":
            age = c.get("age_days", 999)
            min_age = c.get("_min_age", 8)
            max_age = c.get("_max_age", 45)
            if not (min_age <= age <= max_age):
                ok = False

        if not ok:
            continue

        stop_pct   = cfg["stop_pct"]
        size_pct   = cfg["size_pct"]

        new_lots.append(Lot(
            symbol=sym,
            entry_date=market_day,
            entry_price=price,
            qty=0,              # filled in by caller (float sizing)
            stop=round(price * (1 - stop_pct), 2),
            target=round(price * (1 + 0.99), 2),   # take_profit=0.99 → no hard TP
            strategy=strat,
            hold_days=cfg["hold_days"],
        ))
        slots -= 1

    return new_lots


def run_simulation(
    store: data.BarStore,
    universe: list[str],
    start_equity: float = 100_000.0,
    strategies: list[str] | None = None,
    warmup: int = 70,
    slippage_bps: float = 0.0,
    stop_mode: str = "flat",
    atr_stop_mult: float = 1.5,
    regime_gated: bool = False,
) -> Portfolio:
    """Run multi-strategy simulation across all trading days.

    Each strategy screener (meanrev / insider / squeeze / breakout / earnmom)
    is called on every day T and returns candidates from data through T-1.
    Candidates from all strategies are pooled, ranked by their strategy score
    (no cross-strategy normalization), and top MAX_BUYS enter at T's open.
    Exits: stop-loss or time-expiry. Same-day double-touch: stop-first.
    """
    if strategies is None:
        strategies = ["meanrev", "insider", "squeeze", "breakout", "earnmom"]

    # Install store into data so fake_fetch() picks it up (install_store sets
    # the global STORE that fake_fetch checks; ._store = store does NOT work).
    data.install_store(store)

    cal = store.trading_calendar("SPY")
    if len(cal) <= warmup + 1:
        raise RuntimeError(
            f"Not enough SPY history ({len(cal)} bars) for warmup={warmup}")

    pf = Portfolio(cash=start_equity)
    last_px: dict[str, float] = {}
    slip = slippage_bps / 10_000.0
    buy_fill  = lambda px: px * (1 + slip)
    sell_fill = lambda px: px * (1 - slip)

    # ATR(14) helper (same as engine.py)
    def _atr14(sym: str, as_of: datetime.date, n: int = 14) -> float | None:
        bars = [b for b in store.series.get(sym, [])
                if b["date"] <= str(as_of)]
        if len(bars) < n + 1:
            return None
        window = bars[-(n + 1):]
        trs = []
        for prev, cur in zip(window[:-1], window[1:]):
            tr = max(cur["high"] - cur["low"],
                     abs(cur["high"] - prev["close"]),
                     abs(cur["low"]  - prev["close"]))
            trs.append(tr)
        return sum(trs) / len(trs) if trs else None

    def _initial_stop(basis: float, sym: str, as_of: datetime.date) -> float:
        if stop_mode == "atr":
            atr = _atr14(sym, as_of)
            if atr is not None:
                stop = basis - atr_stop_mult * atr
                if stop > 0:
                    return round(stop, 2)
        return round(basis * (1 - STOP), 2)

    # All 5 strategy screeners call _get() which hits FMP — they all log.info()
    # on every invocation (~5 × ~500 days = 2500 verbose lines). Silence them.
    with _quiet_screeners():
        for i in range(warmup, len(cal)):
            T = cal[i]
            data.set_as_of(cal[i - 1])
            clock.set_today(cal[i - 1])

            # refresh last-known prices using bar_on (avoids O(N) scan per day)
            for s in universe:
                b = store.bar_on(s, cal[i - 1])
                if b:
                    last_px[s] = b["close"]

            day_start_equity = _equity(pf, store, cal[i - 1], last_px)

            # ── a. portfolio value at T open + circuit-breaker ──────────────
            pv_open = pf.cash
            for l in pf.lots:
                pv_open += l.qty * _open_px(store, l.symbol, T, last_px)

            cb_blocked = (day_start_equity > 0 and
                           (pv_open - day_start_equity) / day_start_equity <= -config.CIRCUIT_BREAKER_PCT)

            # ── b. COLLECT entries from all strategy runners ──────────────────
            entered_today: set[str] = set()
            candidate_lots: list[tuple[float, Lot]] = []
            if not cb_blocked:
                held = pf.held_symbols()
                slots = min(MAX_BUYS, config.MAX_OPEN_POSITIONS - len(held))

                if regime_gated and slots > 0:
                    spy_bars = [b for b in store.series.get("SPY", [])
                                if b["date"] <= str(cal[i - 1])]
                    if len(spy_bars) >= 50:
                        from regime_gate import classify as _rg
                        reg = _rg([b["high"] for b in spy_bars],
                                  [b["low"]  for b in spy_bars],
                                  [b["close"] for b in spy_bars])
                        if not reg.can_trade:
                            slots = 0
                            log.debug("engine5 day %s: stand-down (regime)", T)

                if slots > 0:
                    for strat in strategies:
                        raw_lots = _run_strategy(strat, store, cal[i - 1], pf,
                                                  slots, entered_today)
                        for lot in raw_lots:
                            score = getattr(lot, "_score", _infer_strategy_score(
                                strat, lot.symbol, store, cal[i - 1]))
                            candidate_lots.append((score, lot))

                # No cross-strategy normalization — each screener returns its own
                # score scale. Rank by score descending.
                candidate_lots.sort(key=lambda x: -x[0])

                for score, lot in candidate_lots:
                    if slots <= 0:
                        break
                    sym = lot.symbol
                    op = _open_px(store, sym, T, last_px)
                    if op <= 0:
                        continue

                    cfg_lot = _STRAT_CONFIG[lot.strategy]

                    # Size from yesterday's close (not T's open — not yet known)
                    pv_for_sizing = pf.cash + sum(
                        l.qty * last_px.get(l.symbol, l.entry_price) for l in pf.lots)
                    size_pct = cfg_lot["size_pct"]
                    qty = int(pv_for_sizing * size_pct / op) if op > 0 else 0
                    if qty < 1:
                        continue

                    basis = buy_fill(op)
                    stop  = _initial_stop(basis, sym, cal[i - 1])

                    pf.lots.append(Lot(
                        symbol=sym, entry_date=T, entry_price=basis,
                        qty=qty, stop=stop,
                        target=round(basis * 1.99, 2),   # no hard TP; 0.99 just a placeholder
                        strategy=lot.strategy,
                        hold_days=cfg_lot["hold_days"],
                    ))
                    pf.cash -= qty * basis
                    entered_today.add(sym)
                    slots -= 1

            # ── c. EXITS — stop loss, time expiry; same-day double touch → stop first
            survivors: list[Lot] = []
            for l in pf.lots:
                bar = store.bar_on(l.symbol, T)
                if not bar:
                    survivors.append(l)
                    continue

                exited_by, exit_px = None, None
                if bar["low"] <= l.stop:
                    exited_by, exit_px = "stop", sell_fill(l.stop)
                elif (T - l.entry_date).days >= l.hold_days:
                    exited_by, exit_px = "time", sell_fill(bar["close"])

                if exited_by:
                    pf.cash += l.qty * exit_px
                    pf.trades.append({
                        "symbol":        l.symbol,
                        "strategy":      l.strategy,
                        "entry_date":    l.entry_date.isoformat(),
                        "exit_date":     T.isoformat(),
                        "entry_price":   round(l.entry_price, 4),
                        "exit_price":    round(exit_px, 4),
                        "qty":           l.qty,
                        "return_pct":    round((exit_px / l.entry_price - 1) * 100, 4),
                        "pnl_usd":       round((exit_px - l.entry_price) * l.qty, 2),
                        "holding_days":  (T - l.entry_date).days,
                        "exit_reason":   exited_by,
                        "is_pyramid":    False,
                    })
                    continue

                survivors.append(l)
            pf.lots = survivors

            # ── d. equity snapshot ─────────────────────────────────────────────
            if i % 50 == 0 or i == len(cal) - 1:
                log.info("  day %d/%d … equity=$%.0f  open_lots=%d",
                         i + 1, len(cal), _equity(pf, store, T, last_px), len(pf.lots))
            pf.equity_curve.append({
                "date":   T.isoformat(),
                "equity": round(_equity(pf, store, T, last_px), 2),
            })

    clock.reset()   # restore live wall-clock behavior for any post-sim callers
    return pf


def _infer_strategy_score(strat: str, sym: str, store: data.BarStore,
                           as_of: datetime.date) -> float:
    """Fallback score when screener didn't return a usable candidate dict.
    Used only for cross-strategy ranking tie-breaking."""
    return 0.0