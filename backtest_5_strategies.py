#!/usr/bin/env python3
"""Multi-strategy backtest — meanrev, insider, squeeze, breakout, earnmom.

Mirrors market_open.py entry rules verbatim (same screeners, same filters, same
slot-sharing); outputs per-strategy and aggregate metrics vs SPY.

Usage:
    python backtest_5_strategies.py --strategy all --years 4
    python backtest_5_strategies.py --strategy meanrev,squeeze --years 4
    python backtest_5_strategies.py --strategy all --years 4 --slippage-bps 10

Flags shared with run_backtest.py:
    --years           Backtest window (default: 2.0)
    --force-refresh   Re-fetch bars from Alpaca
    --start-equity    Starting portfolio value (default: 100_000)
    --slippage-bps    Slippage per fill in basis points (default: 0)
    --stop-mode       flat | atr (default: flat)
    --regime-gated    Apply SPY regime stand-down gate (default: off)
    --suite           Run predefined multi-strategy scenarios

Requires: yfinance (for historical bars), core.config.SP80_UNIVERSE.
No FMP calls in this script (all FMP traffic is intercepted by engine5).
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys

# Must disable FMP before any core import (engine5 patches FMP at import time).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_harness"))
os.environ.setdefault("COMPOSITE_USE_FMP", "false")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest5")

REPO = os.path.dirname(os.path.abspath(__file__))


def build_universe(store, min_years: float) -> list[str]:
    """SP80_UNIVERSE filtered to names with >= min_years of daily history."""
    from core.config import SP80_UNIVERSE
    cal = store.trading_calendar("SPY")
    if not cal:
        raise RuntimeError("No SPY bars — check data.")
    need_start = cal[0] + datetime.timedelta(days=5)
    have, dropped = [], []
    for s in SP80_UNIVERSE:
        first = store.first_date(s)
        if first is not None and first <= need_start:
            have.append(s)
        else:
            dropped.append(s)
    log.info("Universe: %d/%d SP80 names with %.0fy history  |  dropped: %s",
             len(have), len(SP80_UNIVERSE), min_years, dropped[:5])
    return have


# Predefined multi-strategy scenarios
MULTI_SUITE = [
    ("all_strategies_slip0_flat",      ["meanrev", "insider", "squeeze", "breakout", "earnmom"],
     0, "flat",   False),
    ("all_strategies_slip10_flat",     ["meanrev", "insider", "squeeze", "breakout", "earnmom"],
     10, "flat",   False),
    ("all_strategies_slip10_atr",      ["meanrev", "insider", "squeeze", "breakout", "earnmom"],
     10, "atr",    False),
    ("all_strategies_slip10_atr_regime", ["meanrev", "insider", "squeeze", "breakout", "earnmom"],
     10, "atr",    True),
    # Strategy-solo variants (subset = single, useful for per-strategy attribution)
    ("meanrev_only",                   ["meanrev"],
     10, "flat",  False),
    ("squeeze_only",                   ["squeeze"],
     10, "flat",  False),
    ("earnmom_only",                   ["earnmom"],
     10, "flat",  False),
]


def _run_scenario(
    store,
    universe: list[str],
    cfg,
    out_dir: str,
    start_equity: float,
    scenario: str,
    strategies: list[str],
    slippage_bps: float,
    stop_mode: str,
    atr_stop_mult: float = 1.5,
    regime_gated: bool = False,
):
    from backtest_harness import engine5 as e5, metrics

    gate_tag = " | regime-gated" if regime_gated else ""
    strategies_str = ", ".join(strategies)
    log.info("── Scenario %s | strategies=[%s] | slippage=%dbps | stop=%s%s ──",
             scenario, strategies_str, slippage_bps, stop_mode, gate_tag)

    pf = e5.run_simulation(
        store, universe,
        start_equity=start_equity,
        strategies=strategies,
        warmup=70,
        slippage_bps=slippage_bps,
        stop_mode=stop_mode,
        atr_stop_mult=atr_stop_mult,
        regime_gated=regime_gated,
    )

    if not pf.equity_curve:
        log.error("Scenario %s produced no equity curve.", scenario)
        return None

    start_d, end_d = pf.equity_curve[0]["date"], pf.equity_curve[-1]["date"]
    strat = metrics.equity_stats(pf.equity_curve, "Multi-strategy")
    tstats = metrics.trade_stats(pf.trades)
    spy_curve = metrics.spy_buy_hold(store, start_d, end_d, start_equity)
    spy = metrics.equity_stats(spy_curve, "SPY B&H") if spy_curve else {}

    # ── Per-strategy attribution ────────────────────────────────────────────────
    # Break out trades by strategy label (engine5.tagged returns "strategy" field)
    strat_pnl: dict[str, dict] = {}
    for strat_key in strategies:
        s_trades = [t for t in pf.trades if t.get("strategy", "") == strat_key]
        if s_trades:
            wins = [t for t in s_trades if t["return_pct"] > 0]
            strat_pnl[strat_key] = {
                "n":       len(s_trades),
                "wins":    len(wins),
                "win_pct": round(len(wins) / len(s_trades) * 100, 1),
                "avg_ret": round(sum(t["return_pct"] for t in s_trades) / len(s_trades), 2),
                "total_pnl": round(sum(t["pnl_usd"] for t in s_trades), 2),
                "avg_hold": round(sum(t["holding_days"] for t in s_trades) / len(s_trades), 1),
            }
        else:
            strat_pnl[strat_key] = {"n": 0}

    png_path = os.path.join(out_dir, f"equity_{scenario}.png")
    metrics.plot_equity(pf.equity_curve, spy_curve, png_path,
                        f"{scenario} :: {strategies_str}")

    stop_desc = ("flat 2% (STOP_LOSS_PCT)" if stop_mode == "flat"
                 else f"ATR(14) × {atr_stop_mult}")

    report = {
        "generated":   datetime.datetime.now().isoformat(timespec="seconds"),
        "scenario":    scenario,
        "config": {
            "strategies":    strategies,
            "slippage_bps":   slippage_bps,
            "stop_mode":      stop_mode,
            "stop_rule":      stop_desc,
            "atr_stop_mult":  atr_stop_mult,
            "regime_gated":   regime_gated,
            "unit":           "as-wired-today (market_open.py)",
            "fmp":            "patched to BarStore (no look-ahead)",
            "window":         {"start": start_d, "end": end_d},
            "start_equity":   start_equity,
            "params": {
                "MAX_POSITION_SIZE_PCT": float(cfg.MAX_POSITION_SIZE_PCT),
                "MAX_OPEN_POSITIONS":    int(cfg.MAX_OPEN_POSITIONS),
                "MAX_BUYS_PER_DAY":     e5.MAX_BUYS,
                "CIRCUIT_BREAKER_PCT":   float(cfg.CIRCUIT_BREAKER_PCT),
                "MEANREV_STOP_PCT":      float(cfg.MEANREV_STOP_PCT),
                "INSIDER_STOP_PCT":      float(cfg.INSIDER_STOP_PCT),
                "SQUEEZE_STOP_PCT":      float(cfg.SQUEEZE_STOP_PCT),
                "BREAKOUT_STOP_PCT":      float(cfg.BREAKOUT_STOP_PCT),
                "EARNMOM_STOP_PCT":       float(cfg.EARNMOM_STOP_PCT),
            },
        },
        "strategy":      strat,
        "spy_buy_hold":  spy,
        "trade_stats":   tstats,
        "per_strategy":  strat_pnl,
        "equity_curve": pf.equity_curve,
        "trades":        pf.trades,
    }

    path = os.path.join(out_dir, f"baseline_{scenario}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)

    _print_summary(scenario, strategies_str, stop_desc, strat, spy, tstats, path, png_path, strat_pnl)
    _run_validation_gates(pf, spy_curve)
    return report


def _run_validation_gates(pf, spy_curve):
    from validation_gates import run_gates

    def _drets(curve):
        eq = [p["equity"] for p in (curve or [])]
        return [(b / a - 1) for a, b in zip(eq[:-1], eq[1:]) if a > 0]

    s_r = _drets(pf.equity_curve)
    sp_r = _drets(spy_curve)
    if len(s_r) < 2:
        log.warning("Validation gates skipped: insufficient data")
        return
    rep = run_gates(strat_daily_returns=s_r, spy_daily_returns=sp_r, n_trades=len(pf.trades))
    print(rep.summary())


def _load_state():
    return REPO, REPO


# ─────────────────────────────────────────────────────────────────────────────
# PEAD-style per-strategy validation (Sharpe / win-rate / p-value / verdict)
#
# Ported verbatim from the standalone scaffold's compute_trade_stats/_verdict so
# the numbers use the exact same methodology already applied to PEAD. The trade
# returns fed in here come from the REAL engine5 simulation (which mirrors
# market_open.py's _run_<strategy> entry rules and the mechanical stop/time
# exits), run one strategy at a time so each strategy's edge is measured on its
# own — no cross-strategy MAX_BUYS slot competition.
# ─────────────────────────────────────────────────────────────────────────────
def compute_trade_stats(returns):
    """Given a list/array of per-trade % returns, compute the validation metrics."""
    import numpy as np
    from scipy import stats

    returns = np.array(returns, dtype=float)
    n = len(returns)

    if n < 5:
        return {
            "trades": int(n),
            "error": "Insufficient trades for meaningful statistics (need 30+ ideally)",
        }

    mean_ret = np.mean(returns)
    std_ret = np.std(returns, ddof=1)
    sharpe = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0.0
    win_rate = np.mean(returns > 0) * 100

    # One-sample t-test: is mean return significantly different from 0?
    t_stat, p_value = stats.ttest_1samp(returns, 0)

    # Max drawdown on cumulative equity curve
    cum_returns = np.cumprod(1 + returns / 100)
    running_max = np.maximum.accumulate(cum_returns)
    drawdown = (cum_returns - running_max) / running_max
    max_dd = np.min(drawdown) * 100

    return {
        "trades": int(n),
        "sharpe": round(float(sharpe), 3),
        "win_rate_pct": round(float(win_rate), 1),
        "p_value": round(float(p_value), 4),
        "avg_trade_return_pct": round(float(mean_ret), 3),
        "max_drawdown_pct": round(float(max_dd), 2),
        "statistically_significant": bool(p_value < 0.05),
        "verdict": _verdict(sharpe, p_value, n),
    }


def _verdict(sharpe, p_value, n):
    if n < 30:
        return "INSUFFICIENT_SAMPLE — need more trades before trusting this"
    if p_value >= 0.05:
        return "NOT SIGNIFICANT — edge could be noise, do not scale capital"
    if sharpe < 0.5:
        return "WEAK — statistically real but economically marginal"
    if sharpe < 1.0:
        return "MODERATE — tradeable but size conservatively"
    return "STRONG — comparable to your validated PEAD edge"


def _run_validation(
    store,
    universe: list[str],
    out_dir: str,
    start_equity: float,
    strategies: list[str],
    slippage_bps: float,
    stop_mode: str,
    atr_stop_mult: float = 1.5,
    regime_gated: bool = False,
) -> dict:
    """Run each strategy SOLO through engine5 and score its trades PEAD-style.

    Solo runs (strategies=[one]) remove cross-strategy slot competition so each
    strategy's win-rate / Sharpe / p-value reflects only its own signal, exactly
    as PEAD was validated on its own.
    """
    from backtest_harness import engine5 as e5

    today = datetime.date.today().isoformat()[:10]
    summary: dict[str, dict] = {}

    for strat in strategies:
        log.info("── Validating %s (solo) | slippage=%dbps | stop=%s%s ──",
                 strat, slippage_bps, stop_mode,
                 " | regime-gated" if regime_gated else "")
        pf = e5.run_simulation(
            store, universe,
            start_equity=start_equity,
            strategies=[strat],
            warmup=70,
            slippage_bps=slippage_bps,
            stop_mode=stop_mode,
            atr_stop_mult=atr_stop_mult,
            regime_gated=regime_gated,
        )

        rets = [t["return_pct"] for t in pf.trades if t.get("strategy") == strat]
        st = compute_trade_stats(rets)
        st["strategy"] = strat
        st["run_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        st["universe_size"] = len(universe)
        st["config"] = {
            "slippage_bps": slippage_bps,
            "stop_mode": stop_mode,
            "atr_stop_mult": atr_stop_mult,
            "regime_gated": regime_gated,
            "engine": "engine5 (mirrors market_open.py entries + mechanical "
                      "stop/time exits; discretionary market_close.py exits "
                      "-3% force-close / cash-regime / Claude-review not modeled)",
        }
        if pf.equity_curve:
            st["window"] = {"start": pf.equity_curve[0]["date"],
                            "end": pf.equity_curve[-1]["date"]}

        path = os.path.join(out_dir, f"validation_{strat}_{today}.json")
        with open(path, "w") as f:
            json.dump(st, f, indent=2)
        summary[strat] = st

    _print_validation_summary(summary, out_dir, today)
    return summary


def _print_validation_summary(summary: dict, out_dir: str, today: str) -> None:
    line = "=" * 78
    print("\n" + line)
    print("  PER-STRATEGY VALIDATION — engine5 solo runs, PEAD methodology")
    print(line)
    print(f"  {'strategy':<10} {'trades':>7} {'win%':>7} {'sharpe':>8} "
          f"{'p-value':>9} {'avg%':>7}  verdict")
    print("  " + "-" * 74)
    for strat, st in summary.items():
        if "error" in st:
            print(f"  {strat:<10} {st.get('trades', 0):>7}   "
                  f"{'—':>5}  {'—':>8} {'—':>9} {'—':>7}  {st['error']}")
            continue
        verdict = st["verdict"].split(" — ")[0]
        print(f"  {strat:<10} {st['trades']:>7} {st['win_rate_pct']:>7} "
              f"{st['sharpe']:>8} {st['p_value']:>9} "
              f"{st['avg_trade_return_pct']:>7}  {verdict}")
    print("  " + "-" * 74)

    combined = os.path.join(out_dir, f"validation_summary_{today}.json")
    with open(combined, "w") as f:
        json.dump(summary, f, indent=2)
    print(_row("Summary JSON", os.path.relpath(combined, REPO)))
    print("  Note: Sharpe uses the scaffold's per-trade × √252 PEAD convention "
          "(not per-day annualization).")
    print(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy",
                    default="all",
                    help="'all' for all-5, or comma-separated list "
                         "(meanrev,insider,squeeze,breakout,earnmom)")
    ap.add_argument("--years",         type=float, default=2.0)
    ap.add_argument("--force-refresh", action="store_true")
    ap.add_argument("--start-equity",  type=float, default=100_000.0)
    ap.add_argument("--slippage-bps",  type=float, default=0.0)
    ap.add_argument("--stop-mode",     choices=["flat", "atr"], default="flat")
    ap.add_argument("--atr-stop-mult", type=float, default=1.5)
    ap.add_argument("--regime-gated", action="store_true")
    ap.add_argument("--suite",        action="store_true",
                    help="run the multi-strategy scenario suite (5 scenarios)")
    ap.add_argument("--validate",     action="store_true",
                    help="PEAD-style per-strategy validation: run each strategy "
                         "solo through engine5 and report Sharpe / win-rate / "
                         "p-value / verdict from the real trade returns")
    ap.add_argument("--yf",           action="store_true",
                    help="fetch bars via yfinance (4y+ history, no API key, "
                         "split/div-adjusted) instead of Alpaca IEX (~2y cap). "
                         "Use for genuine multi-year windows.")
    ap.add_argument("--out-dir",      type=str, default=None,
                    help="output directory (default: backtests/multi_<date>/")
    args = ap.parse_args()

    # ── Resolve strategies ────────────────────────────────────────────────────
    if args.strategy == "all":
        active = ["meanrev", "insider", "squeeze", "breakout", "earnmom"]
    else:
        active = [s.strip() for s in args.strategy.split(",") if s.strip()]
        valid  = {"meanrev", "insider", "squeeze", "breakout", "earnmom"}
        bad    = [s for s in active if s not in valid]
        if bad:
            log.error("Unknown strategy(s): %s — valid: %s", bad, sorted(valid))
            return 1

    # ── Gather data ──────────────────────────────────────────────────────────
    from backtest_harness import data as hd
    from core import config as cfg
    from core.config import SP80_UNIVERSE

    cache_symbols = list(dict.fromkeys(SP80_UNIVERSE + ["SPY"] + hd.INDEX_SYMBOLS))

    # Need extra days for the largest lookback (meanrev needs ~200d lookback)
    if args.yf:
        log.info("Data source: yfinance (4y+ history, split/div-adjusted)")
        series = hd.fetch_and_cache_yf(cache_symbols, years=args.years + 1.0,
                                       force=args.force_refresh)
    else:
        log.info("Data source: Alpaca IEX (~2y cap; use --yf for longer windows)")
        series = hd.fetch_and_cache(cache_symbols, years=args.years + 1.0,
                                    force=args.force_refresh)
    if "SPY" not in series:
        log.error("No SPY data — check connectivity / yfinance. Aborting.")
        return 1

    store = hd.BarStore(series)
    hd.install_store(store)

    universe = build_universe(store, args.years)
    if not universe:
        log.error("Empty universe after history filter — aborting.")
        return 1

    # ── earnmom: point-in-time earnings (fetch BEFORE engine5 patches FMP) ────
    if "earnmom" in active:
        from backtest_harness import fundamentals
        log.info("Fetching historical earnings for earnmom (point-in-time)…")
        earn_store = fundamentals.fetch_and_cache_earnings(
            SP80_UNIVERSE, force=args.force_refresh)
        from backtest_harness import engine5 as _e5
        _e5.install_earnings_store(earn_store)

    today = datetime.date.today().isoformat()[:10]
    out_dir = args.out_dir or os.path.join(REPO, "backtests", f"multi_{today}")
    os.makedirs(out_dir, exist_ok=True)

    if args.validate:
        log.info("Validation mode: %s | %d symbols | ~%.1fy window (solo runs)",
                 active, len(universe), args.years)
        summary = _run_validation(
            store, universe, out_dir, args.start_equity, active,
            int(args.slippage_bps), args.stop_mode,
            atr_stop_mult=args.atr_stop_mult, regime_gated=args.regime_gated)
        return 0 if summary else 1

    if args.suite:
        scenarios = MULTI_SUITE
    else:
        scenarios = [(f"{'-'.join(active)}_slip{int(args.slippage_bps)}_{args.stop_mode}",
                       active, int(args.slippage_bps), args.stop_mode, args.regime_gated)]

    log.info("Strategies: %s | %d symbols | ~%.1fy window | %d scenario(s)",
             active, len(universe), args.years, len(scenarios))

    ok = False
    for name, strats, slip, mode, gated in scenarios:
        if _run_scenario(store, universe, cfg, out_dir, args.start_equity,
                         name, strats, slip, mode,
                         atr_stop_mult=args.atr_stop_mult,
                         regime_gated=gated) is not None:
            ok = True

    return 0 if ok else 1


def _row(label, val):
    return f"  {label:<30} {val}"


def pct(x):
    if x is None or x == 'n/a':
        return 'n/a'
    return f"{x:+.2f}%" if isinstance(x, (int, float)) else str(x)


def _print_summary(scenario, strats, stop_desc, strat, spy, t, json_path,
                   png_path, per_strat: dict) -> None:
    line = "=" * 70
    print("\n" + line)
    print(f"  SCENARIO {scenario}")
    print(f"  strategies=[{strats}]  slippage={t.get('slippage_bps',0)}bps"
          f"  stop={stop_desc}  as-wired-today")
    print(line)
    print("  RETURNS                       STRATEGY        SPY B&H")
    print(f"  {'Total return':<30} {pct(strat.get('total_return_pct') or 0):>12}"
          f"   {pct(spy.get('total_return_pct') or 'n/a'):>12}")
    print(f"  {'CAGR':<30} {pct(strat.get('cagr_pct')):>12}"
          f"   {pct(spy.get('cagr_pct','n/a')):>12}")
    print(f"  {'Max drawdown':<30} {pct(strat.get('max_drawdown_pct')):>12}"
          f"   {pct(spy.get('max_drawdown_pct','n/a')):>12}")
    print(f"  {'Sharpe':<30} {strat.get('sharpe') or 'n/a':>12}"
          f"   {str(spy.get('sharpe') or 'n/a'):>12}")
    print(f"  {'Sortino':<30} {strat.get('sortino') or 'n/a':>12}"
          f"   {str(spy.get('sortino') or 'n/a'):>12}")
    print("  " + "-" * 68)
    print("  PER-STRATEGY TRADE ATTRIBUTION")
    for k, v in per_strat.items():
        print(f"  {k:<30} n={v['n']:>3}  win%={v.get('win_pct','—'):>6}"
              f"  avg_ret={pct(v.get('avg_ret',0))}"
              f"  pnl=${v.get('total_pnl',0):>10,.2f}"
              f"  avg_hold={v.get('avg_hold','—')}d")
    print("  " + "-" * 68)
    print("  TRADE STATS")
    print(_row("Trades",                    t.get("num_trades", 0)))
    print(_row("Win rate",                  pct(t.get("win_rate_pct", 0))))
    print(_row("Avg win / avg loss",        f"{pct(t.get('avg_win_pct', 0))} / {pct(t.get('avg_loss_pct', 0))}"))
    print(_row("Win/loss ratio",            t.get("win_loss_ratio", "—")))
    print(_row("Expectancy / trade",        pct(t.get("expectancy_pct_per_trade", 0))))
    print(_row("Avg holding days",          t.get("avg_holding_days", "—")))
    print(_row("Exits stop / time",         f"{t.get('exits_stop','—')} / {t.get('exits_time','—')}"))
    print(_row("Total realized P&L",         f"${t.get('total_pnl_usd', 0):,.2f}"))
    print("  " + "-" * 68)
    print(f"  Window: {strat.get('start_date','?')} → {strat.get('end_date','?')} "
          f"({strat.get('trading_days','?')} trading days)")
    print(_row("JSON",        os.path.relpath(json_path, REPO)))
    print(_row("Equity PNG", os.path.relpath(png_path, REPO)))
    print(line + "\n")


if __name__ == "__main__":
    raise SystemExit(main())