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

# Predefined scenario suite (run with --suite): (name, slippage_bps, stop_mode)
SUITE = [
    ("A_control_slip0_flat2pct", 0, "flat"),
    ("B_slip10_atr", 10, "atr"),
    ("C_slip20_atr", 20, "atr"),
]


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


def _run_one(store, universe, cfg, out_dir, start_equity, scenario, slippage_bps, stop_mode):
    """Run one scenario, write its JSON+PNG, print its table, return the report."""
    from backtest_harness import engine, metrics

    log.info("── Scenario %s | slippage=%dbps | stop=%s ──", scenario, slippage_bps, stop_mode)
    pf = engine.run_simulation(store, universe, start_equity=start_equity,
                               slippage_bps=slippage_bps, stop_mode=stop_mode,
                               atr_stop_mult=ATR_STOP_MULT)
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

    scenarios = SUITE if args.suite else [("single", int(args.slippage_bps), args.stop_mode)]
    log.info("Simulating %d symbols over ~%.1fy | %d scenario(s)", len(universe), args.years, len(scenarios))
    ok = False
    for name, slip, mode in scenarios:
        if _run_one(store, universe, cfg, out_dir, args.start_equity, name, slip, mode) is not None:
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


if __name__ == "__main__":
    raise SystemExit(main())
