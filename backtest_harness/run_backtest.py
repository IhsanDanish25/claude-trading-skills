#!/usr/bin/env python3
"""Baseline backtest of the live composite strategy (READ-ONLY measurement).

Usage:
    .venv/bin/python -m backtest_harness.run_backtest [--years 2] [--force-refresh]

Reuses the production strategy functions verbatim (see engine.py), simulates the
as-wired-today rules with no look-ahead, and writes:
    backtests/baseline_<date>/baseline.json
    backtests/baseline_<date>/equity_curve.png
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys

# Make the repo importable and disable FMP look-ahead before any core import.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPOSITE_USE_FMP", "false")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Tunables for this experiment ──────────────────────────────────────────────
SLIPPAGE_BPS = 0      # default: no slippage. Each fill moves against us by BPS/10000.
ATR_STOP_MULT = 1.5   # ATR-stop multiple: stop = entry - ATR_STOP_MULT * ATR(14)

# Predefined scenario suite (run with --suite): (name, slippage_bps, stop_mode, regime_gated)
# Scenarios A–D run the VCP composite engine (engine.run_simulation).
# Scenario E (name prefix "E_") is the earnings-momentum strategy and is routed
# to its own engine + window via _run_earnings_scenario — see main().
SUITE = [
    ("A_control_slip0_flat2pct", 0, "flat", False),
    ("B_slip10_atr", 10, "atr", False),
    ("C_slip20_atr", 20, "atr", False),
    ("D_slip10_atr_regime", 10, "atr", True),
    # Earnings-momentum scenarios — routed to earnings_engine, not engine.py.
    # (stop_mode field unused for E-series; stop config is in E_CONFIGS below.)
    ("E_earnings_momentum_regime", 10, "atr", True),
    ("E2_earnings_wide_stop",      10, "atr", True),
    ("E3_earnings_time_only",      10, "atr", True),
]

# Scenario E-series shared parameters.
E_WINDOW_START = "2024-01-01"
E_WINDOW_END = "2026-06-26"
E_HOLD_DAYS = 60
E_MIN_SURPRISE_PCT = 5.0   # lowered from 10% — wider net for trade-count gate
E_MIN_PRICE = 10.0
E_MIN_AVG_VOLUME = 500_000.0

# Per-scenario stop configuration for the E-series.
# trailing_stop=False + fixed_stop_pct → flat disaster-protection stop, never ratcheted.
E_CONFIGS: dict[str, dict] = {
    "E_earnings_momentum_regime": {"atr_stop_mult": 1.5, "trailing_stop": True,  "fixed_stop_pct": None},
    "E2_earnings_wide_stop":      {"atr_stop_mult": 3.0, "trailing_stop": True,  "fixed_stop_pct": None},
    "E3_earnings_time_only":      {"atr_stop_mult": 1.5, "trailing_stop": False, "fixed_stop_pct": 0.15},
}


def build_universe(store, years: float) -> list[str]:
    """Decision 1: static watchlist filtered to names with >= `years` of history
    (full point-in-time coverage; no recent-IPO gaps, no survivorship from
    today's dynamic actives)."""
    from core.config import WATCHLIST
    cal = store.trading_calendar("SPY")
    if not cal:
        raise RuntimeError("No SPY bars cached — cannot establish calendar")
    need_start = cal[0] + datetime.timedelta(days=5)  # must cover from window start
    universe = []
    dropped = []
    for s in WATCHLIST:
        first = store.first_date(s)
        if first is not None and first <= need_start:
            universe.append(s)
        else:
            dropped.append((s, first.isoformat() if first else "no-data"))
    log.info("Universe: %d/%d watchlist names with full history; dropped: %s",
             len(universe), len(WATCHLIST), dropped)
    return universe


def _run_one(store, universe, cfg, out_dir, start_equity, scenario, slippage_bps, stop_mode,
             regime_gated=False):
    """Run one scenario, write its JSON+PNG, print its table, return the report."""
    from backtest_harness import engine, metrics

    gate_tag = " | regime-gated" if regime_gated else ""
    log.info("── Scenario %s | slippage=%dbps | stop=%s%s ──", scenario, slippage_bps, stop_mode, gate_tag)
    pf = engine.run_simulation(store, universe, start_equity=start_equity,
                               slippage_bps=slippage_bps, stop_mode=stop_mode,
                               atr_stop_mult=ATR_STOP_MULT, regime_gated=regime_gated)
    if not pf.equity_curve:
        log.error("Scenario %s produced no equity curve — skipping.", scenario)
        return None

    start_date, end_date = pf.equity_curve[0]["date"], pf.equity_curve[-1]["date"]
    strat = metrics.equity_stats(pf.equity_curve, "Composite strategy")
    tstats = metrics.trade_stats(pf.trades)
    spy_curve = metrics.spy_buy_hold(store, start_date, end_date, start_equity)
    spy = metrics.equity_stats(spy_curve, "SPY buy & hold") if spy_curve else {}

    png_path = os.path.join(out_dir, f"equity_curve_{scenario}.png")
    metrics.plot_equity(pf.equity_curve, spy_curve, png_path,
                        f"{scenario}: composite vs SPY  ({start_date} → {end_date})")

    stop_desc = ("flat 2% (STOP_LOSS_PCT)" if stop_mode == "flat"
                 else f"ATR(14) × {ATR_STOP_MULT}")
    report = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "scenario": scenario,
        "config": {
            "slippage_bps": slippage_bps,
            "stop_mode": stop_mode,
            "stop_rule": stop_desc,
            "atr_stop_mult": ATR_STOP_MULT,
            "universe_mode": "watchlist + >=2y history",
            "ruleset": "as-wired-today",
            "fmp": "disabled (COMPOSITE_USE_FMP=false, no look-ahead)",
            "window": {"start": start_date, "end": end_date},
            "start_equity": start_equity,
            "params": {
                "MAX_POSITION_SIZE_PCT": float(cfg.MAX_POSITION_SIZE_PCT),
                "MAX_OPEN_POSITIONS": int(cfg.MAX_OPEN_POSITIONS),
                "MAX_BUYS_PER_DAY": engine.MAX_BUYS,
                "STOP_LOSS_PCT": float(cfg.STOP_LOSS_PCT),
                "TAKE_PROFIT_PCT": float(cfg.TAKE_PROFIT_PCT),
                "TRAIL_STOP_PCT": float(cfg.TRAIL_STOP_PCT),
                "PYRAMID_TRIGGER_PCT": float(cfg.PYRAMID_TRIGGER_PCT),
                "CIRCUIT_BREAKER_PCT": float(cfg.CIRCUIT_BREAKER_PCT),
                "MAX_GAP_PCT": float(cfg.MAX_GAP_PCT),
                "MIN_COMPOSITE_SCORE": int(cfg.MIN_COMPOSITE_SCORE),
            },
            "universe": universe,
        },
        "strategy": strat,
        "spy_buy_hold": spy,
        "trade_stats": tstats,
        "equity_curve": pf.equity_curve,
        "trades": pf.trades,
    }
    json_path = os.path.join(out_dir, f"baseline_{scenario}.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)

    _print_summary(scenario, slippage_bps, stop_desc, strat, spy, tstats, json_path, png_path)
    _run_validation_gates(pf, spy_curve)
    return report


def _run_earnings_scenario(cfg, out_dir, start_equity, scenario, slippage_bps,
                           regime_gated=True):
    """E-series earnings-momentum scenarios — own engine, own universe, own window.

    Stop behaviour is looked up from E_CONFIGS by scenario name:
      E  — ATR(14)×1.5 trailing stop
      E2 — ATR(14)×3.0 trailing stop (wider room to breathe)
      E3 — fixed -15% disaster stop only, no trailing; pure 60d time exit
    engine.run_simulation (scenarios A–D) is not involved."""
    from backtest_harness import data, earnings_data, earnings_engine, metrics

    ecfg = E_CONFIGS.get(scenario, E_CONFIGS["E_earnings_momentum_regime"])
    atr_stop_mult = ecfg["atr_stop_mult"]
    trailing_stop = ecfg["trailing_stop"]
    fixed_stop_pct = ecfg["fixed_stop_pct"]

    gate_tag = " | regime-gated" if regime_gated else ""
    log.info("── Scenario %s | earnings-momentum | slippage=%dbps | hold=%dd | %s..%s%s ──",
             scenario, slippage_bps, E_HOLD_DAYS, E_WINDOW_START, E_WINDOW_END, gate_tag)

    # 1. Historical EPS surprises over the (wide) E window — cached on disk.
    from core.earnings_screener import get_sp500_symbols
    universe_symbols = get_sp500_symbols()
    qualifying = earnings_data.get_historical_surprises(
        universe_symbols, E_WINDOW_START, E_WINDOW_END, min_surprise_pct=E_MIN_SURPRISE_PCT,
    )
    syms = sorted({s["symbol"] for s in qualifying})
    log.info("Scenario %s: %d qualifying surprises (>=%.0f%%), %d unique symbols",
             scenario, len(qualifying), E_MIN_SURPRISE_PCT, len(syms))
    if not syms:
        log.error("Scenario %s: no qualifying earnings surprises — skipping.", scenario)
        return None

    # 2. Daily bars for the reporting names + SPY. years=3.0 covers the
    #    2024-01-01 window start plus warmup/ATR from today (no look-ahead;
    #    the sim only ever reads bars <= as_of).
    need_syms = list(dict.fromkeys(syms + ["SPY"]))
    series = data.fetch_and_cache(need_syms, years=3.0)
    if "SPY" not in series:
        log.error("Scenario %s: no SPY bars — aborting.", scenario)
        return None
    store = data.BarStore(series)
    data.install_store(store)

    # 3. Run the dedicated earnings-momentum simulation.
    pf = earnings_engine.run_earnings_simulation(
        store, qualifying, start_equity=start_equity, slippage_bps=slippage_bps,
        atr_stop_mult=atr_stop_mult, hold_days=E_HOLD_DAYS, regime_gated=regime_gated,
        window_start=E_WINDOW_START, window_end=E_WINDOW_END,
        min_surprise_pct=E_MIN_SURPRISE_PCT, min_price=E_MIN_PRICE,
        min_avg_volume=E_MIN_AVG_VOLUME,
        trailing_stop=trailing_stop, fixed_stop_pct=fixed_stop_pct,
    )
    if not pf.equity_curve:
        log.error("Scenario %s produced no equity curve — skipping.", scenario)
        return None

    start_date, end_date = pf.equity_curve[0]["date"], pf.equity_curve[-1]["date"]
    strat = metrics.equity_stats(pf.equity_curve, "Earnings momentum")
    tstats = metrics.trade_stats(pf.trades)
    spy_curve = metrics.spy_buy_hold(store, start_date, end_date, start_equity)
    spy = metrics.equity_stats(spy_curve, "SPY buy & hold") if spy_curve else {}

    png_path = os.path.join(out_dir, f"equity_curve_{scenario}.png")
    metrics.plot_equity(pf.equity_curve, spy_curve, png_path,
                        f"{scenario}: earnings momentum vs SPY  ({start_date} → {end_date})")

    if fixed_stop_pct is not None:
        stop_desc = f"-{fixed_stop_pct*100:.0f}% fixed stop + {E_HOLD_DAYS}d time stop (no trailing)"
        stop_mode_tag = "fixed_flat"
    elif trailing_stop:
        stop_desc = f"ATR(14) × {atr_stop_mult} trailing + {E_HOLD_DAYS}d time stop"
        stop_mode_tag = "atr_trailing"
    else:
        stop_desc = f"{E_HOLD_DAYS}d time stop only"
        stop_mode_tag = "time_only"
    report = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "scenario": scenario,
        "config": {
            "strategy": "earnings_momentum",
            "slippage_bps": slippage_bps,
            "stop_mode": stop_mode_tag,
            "stop_rule": stop_desc,
            "atr_stop_mult": atr_stop_mult,
            "trailing_stop": trailing_stop,
            "fixed_stop_pct": fixed_stop_pct,
            "hold_days": E_HOLD_DAYS,
            "entry_rule": f"buy next-open after EPS surprise >= {E_MIN_SURPRISE_PCT}%",
            "regime_gated": regime_gated,
            "liquidity_filter": {"min_price": E_MIN_PRICE, "min_avg_volume": E_MIN_AVG_VOLUME},
            "signal_source": "yfinance Ticker.get_earnings_dates (S&P 500 universe)",
            "note": "no look-ahead: entry is next trading day open after report date",
            "window": {"start": start_date, "end": end_date},
            "start_equity": start_equity,
            "n_qualifying_surprises": len(qualifying),
            "n_universe_symbols": len(syms),
            "params": {
                "MAX_POSITION_SIZE_PCT": float(cfg.MAX_POSITION_SIZE_PCT),
                "MAX_OPEN_POSITIONS": int(cfg.MAX_OPEN_POSITIONS),
                "STOP_LOSS_PCT": float(cfg.STOP_LOSS_PCT),
            },
        },
        "strategy": strat,
        "spy_buy_hold": spy,
        "trade_stats": tstats,
        "equity_curve": pf.equity_curve,
        "trades": pf.trades,
    }
    json_path = os.path.join(out_dir, f"baseline_{scenario}.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)

    _print_summary(scenario, slippage_bps, stop_desc, strat, spy, tstats, json_path, png_path)
    _run_validation_gates(pf, spy_curve)
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--force-refresh", action="store_true", help="re-fetch bars from Alpaca")
    ap.add_argument("--start-equity", type=float, default=100_000.0)
    ap.add_argument("--slippage-bps", type=float, default=SLIPPAGE_BPS)
    ap.add_argument("--stop-mode", choices=["flat", "atr"], default="flat")
    ap.add_argument("--suite", action="store_true",
                    help="run the predefined 3-scenario suite (control / slip10+atr / slip20+atr)")
    ap.add_argument("--scenario", type=str, default=None,
                    help="run only the named scenario or prefix (e.g. E or E_earnings_momentum_regime)")
    args = ap.parse_args()

    from backtest_harness import data
    from core import config as cfg
    from core.config import WATCHLIST

    # Cache historical bars ONCE (watchlist + index + sector ETFs); +0.4y for warmup.
    symbols = list(dict.fromkeys(WATCHLIST + data.INDEX_SYMBOLS + data.SECTOR_ETFS))
    series = data.fetch_and_cache(symbols, years=args.years + 0.4, force=args.force_refresh)
    if "SPY" not in series:
        log.error("No SPY bars — check Alpaca credentials / connectivity. Aborting.")
        return 1
    store = data.BarStore(series)
    data.install_store(store)

    universe = build_universe(store, args.years)
    if not universe:
        log.error("Empty universe after history filter — aborting.")
        return 1

    today = datetime.date.today().isoformat()
    out_dir = os.path.join(REPO, "backtests", f"baseline_{today}")
    os.makedirs(out_dir, exist_ok=True)

    if args.suite:
        scenarios = SUITE
    elif args.scenario:
        prefix = args.scenario.upper().rstrip("_")
        # Single-letter prefix (e.g. "E") matches the whole family (E, E2, E3).
        # Multi-char prefix (e.g. "E2") matches only that scenario.
        if len(prefix) == 1:
            scenarios = [s for s in SUITE if s[0].upper().startswith(prefix)]
        else:
            scenarios = [s for s in SUITE if s[0].startswith(prefix + "_") or s[0].upper() == prefix]
        if not scenarios:
            log.error("No scenario in SUITE matching %r — valid names: %s",
                      args.scenario, [s[0] for s in SUITE])
            return 1
    else:
        scenarios = [("single", int(args.slippage_bps), args.stop_mode, False)]
    log.info("Simulating %d symbols over ~%.1fy | %d scenario(s)", len(universe), args.years, len(scenarios))
    ok = False
    for name, slip, mode, gated in scenarios:
        if name.startswith("E"):
            # Earnings-momentum scenario (E, E2, E3): own engine, own universe, own window.
            if _run_earnings_scenario(cfg, out_dir, args.start_equity, name, slip,
                                      regime_gated=gated) is not None:
                ok = True
            continue
        if _run_one(store, universe, cfg, out_dir, args.start_equity, name, slip, mode,
                    regime_gated=gated) is not None:
            ok = True
    return 0 if ok else 1


def _row(label: str, val) -> str:
    return f"  {label:<28} {val}"


def _print_summary(scenario, slippage_bps, stop_desc, strat, spy, t, json_path, png_path) -> None:
    def pct(x):
        return f"{x:+.2f}%" if isinstance(x, (int, float)) else str(x)

    line = "=" * 64
    print("\n" + line)
    print(f"  SCENARIO {scenario}")
    print(f"  slippage={slippage_bps}bps | stop={stop_desc} | TP=6% | as-wired-today")
    print(line)
    print("  RETURNS                       STRATEGY        SPY B&H")
    sr, br = strat, (spy or {})
    print(f"  {'Total return':<28} {pct(sr.get('total_return_pct')):>12}   {pct(br.get('total_return_pct','n/a')):>12}")
    print(f"  {'CAGR':<28} {pct(sr.get('cagr_pct')):>12}   {pct(br.get('cagr_pct','n/a')):>12}")
    print(f"  {'Max drawdown':<28} {pct(sr.get('max_drawdown_pct')):>12}   {pct(br.get('max_drawdown_pct','n/a')):>12}")
    print(f"  {'Sharpe':<28} {str(sr.get('sharpe')):>12}   {str(br.get('sharpe','n/a')):>12}")
    print(f"  {'Sortino':<28} {str(sr.get('sortino')):>12}   {str(br.get('sortino','n/a')):>12}")
    print("  " + "-" * 60)
    print("  TRADE STATS")
    print(_row("Trades (incl. pyramids)", f"{t.get('num_trades')}  (pyramid adds: {t.get('pyramid_trades', 0)})"))
    print(_row("Win rate", pct(t.get('win_rate_pct'))))
    print(_row("Avg win / avg loss", f"{pct(t.get('avg_win_pct'))} / {pct(t.get('avg_loss_pct'))}"))
    print(_row("Win/loss ratio", t.get('win_loss_ratio')))
    print(_row("Expectancy / trade", pct(t.get('expectancy_pct_per_trade'))))
    print(_row("Avg holding days", t.get('avg_holding_days')))
    print(_row("Exits stop / target", f"{t.get('exits_stop')} / {t.get('exits_target')}"))
    print(_row("Total realized P&L", f"${t.get('total_pnl_usd', 0):,.2f}"))
    print("  " + "-" * 60)
    print(f"  Window: {sr.get('start_date')} -> {sr.get('end_date')} ({sr.get('trading_days')} trading days)")
    print(_row("JSON", os.path.relpath(json_path, REPO)))
    print(_row("Equity PNG", os.path.relpath(png_path, REPO)))
    print(line + "\n")


def _curve_to_daily_returns(curve):
    """Convert oldest->newest equity curve to daily return decimals."""
    eq = [p["equity"] for p in (curve or [])]
    return [(b / a - 1) for a, b in zip(eq[:-1], eq[1:]) if a > 0]


def _run_validation_gates(pf, spy_curve):
    """Bolt validation_gates onto the harness output. Harness logic is unchanged;
    this only reads pf.equity_curve and spy_curve already produced by the run."""
    from validation_gates import run_gates
    strat_rets = _curve_to_daily_returns(pf.equity_curve)
    spy_rets   = _curve_to_daily_returns(spy_curve)
    if len(strat_rets) != len(spy_rets) or len(strat_rets) < 2:
        log.warning("Validation gates skipped: curve length mismatch strat=%d spy=%d",
                    len(strat_rets), len(spy_rets))
        return
    rep = run_gates(
        strat_daily_returns=strat_rets,
        spy_daily_returns=spy_rets,
        n_trades=len(pf.trades),
        is_return=None,
        oos_return=None,
    )
    print(rep.summary())


if __name__ == "__main__":
    raise SystemExit(main())
