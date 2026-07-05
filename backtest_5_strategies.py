#!/usr/bin/env python3
"""Point-in-time backtest of 5 satellite strategies (READ-ONLY measurement).

Usage:
    python3 backtest_5_strategies.py [--start-equity 100000]

Reuses the existing backtest_harness (earnings_engine sim loop, metrics,
validation_gates) — see backtest_harness/satellite_signals.py for the new
point-in-time Breakout / Mean Reversion signal generators.

Data notice:
  Breakout and Mean Reversion are computed entirely from the committed OHLCV bar
  cache (backtest_harness/cache/*.json) — no network. Earnings Momentum adds
  yfinance earnings dates via backtest_harness/earnings_data.py (disk-cached;
  needs network on first run per symbol). Insider Buying and Short Squeeze stay
  unbacktestable — their FMP endpoints (/stable/insider-trading,
  /stable/short-interest) are paid-tier (402/403 on the free plan), reported as
  "blocked_paid_tier".

Writes:
    backtests/five_strategies_<date>/{breakout,meanrev,earnmom}.json
    backtests/five_strategies_<date>/summary.json
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("COMPOSITE_USE_FMP", "false")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest.five_strategies")

REPO = os.path.dirname(os.path.abspath(__file__))

# Strategy configs, pulled straight from core.config (same values live trading uses).
STRATEGY_CONFIGS = {
    "breakout": {
        "label": "Breakout",
        "stop_pct_attr": "BREAKOUT_STOP_PCT",
        "hold_days_attr": "BREAKOUT_HOLD_DAYS",
        "min_price_attr": "BREAKOUT_MIN_PRICE",
        "min_avg_volume_attr": "BREAKOUT_MIN_AVG_VOLUME",
        "min_score": 0.0,
    },
    "meanrev": {
        "label": "Mean Reversion",
        "stop_pct_attr": "MEANREV_STOP_PCT",
        "hold_days_attr": "MEANREV_HOLD_DAYS",
        "min_price_attr": "MEANREV_MIN_PRICE",
        "min_avg_volume_attr": "MEANREV_MIN_AVG_VOLUME",
        "min_score": 0.0,
    },
    "earnmom": {
        "label": "Earnings Momentum",
        "stop_pct_attr": "EARNMOM_STOP_PCT",
        "hold_days_attr": "EARNMOM_HOLD_DAYS",
        "min_price_attr": "EARNMOM_MIN_PRICE",
        "min_avg_volume_attr": "EARNMOM_MIN_AVG_VOLUME",
        "min_score": 0.0,
    },
}

# insider/squeeze are blocked by FMP PLAN (not network): /stable/insider-trading
# and /stable/short-interest return 402/403 on the free tier even with network.
# Confirmed against both the local and production FMP keys. Earnings Momentum is
# no longer blocked — earnings dates come from yfinance via earnings_data.py.
BLOCKED_STRATEGIES = {
    "insider": {
        "label": "Insider Buying",
        "reason": "needs FMP /stable/insider-trading history — paid-tier endpoint "
                  "(returns 402 Payment Required on the free plan, with or without network)",
    },
    "squeeze": {
        "label": "Short Squeeze",
        "reason": "needs FMP /stable/short-interest history — not on the free plan "
                  "(404/403); even paid FMP is only bi-monthly FINRA snapshots",
    },
}


def build_universe(store) -> list[str]:
    """Tradable universe: every symbol cached on disk minus index/sector ETFs.

    Deviation from live: production screens core.config.SP80_UNIVERSE (103
    symbols); only 11 of those are present in the local bar cache. Rather than
    silently narrowing to that thin overlap, this uses the full 30-symbol
    cached universe (mega-cap tech + growth names) so the sample size supports
    a meaningful trade count. See summary.json methodology note.
    """
    from backtest_harness import data
    exclude = set(data.INDEX_SYMBOLS) | set(data.SECTOR_ETFS)
    return sorted(s for s in store.series if s not in exclude)


def _run_satellite_strategy(key: str, store, universe, start_equity: float,
                             slippage_bps: float, out_dir: str) -> dict:
    """Run one satellite strategy (breakout/meanrev) end-to-end and return its report."""
    from backtest_harness import earnings_engine, metrics, satellite_signals
    from core import config as cfg
    from validation_gates import run_gates

    scfg = STRATEGY_CONFIGS[key]
    label = scfg["label"]
    stop_pct = getattr(cfg, scfg["stop_pct_attr"])
    hold_days = getattr(cfg, scfg["hold_days_attr"])
    min_price = getattr(cfg, scfg["min_price_attr"])
    min_avg_volume = getattr(cfg, scfg["min_avg_volume_attr"])

    cal = store.trading_calendar("SPY")
    window_start, window_end = cal[0], cal[-1]

    log.info("── %s | fixed stop=%.0f%% | hold=%dd | %s..%s ──",
             label, stop_pct * 100, hold_days, window_start, window_end)

    if key == "breakout":
        signals = satellite_signals.get_historical_breakout_signals(
            store, universe, window_start, window_end)
    elif key == "earnmom":
        signals = satellite_signals.get_historical_earnmom_signals(
            store, universe, window_start, window_end)
    else:
        signals = satellite_signals.get_historical_meanrev_signals(
            store, universe, window_start, window_end)
    log.info("%s: %d point-in-time signals across %d symbols",
             label, len(signals), len({s['symbol'] for s in signals}))
    if not signals:
        return {"label": label, "status": "no_signals",
                "reason": f"no {label} signals generated from the cached bar history"}

    pf = earnings_engine.run_earnings_simulation(
        store, signals, start_equity=start_equity, slippage_bps=slippage_bps,
        hold_days=hold_days, regime_gated=True,
        window_start=window_start.isoformat(), window_end=window_end.isoformat(),
        min_surprise_pct=0.0, min_price=min_price, min_avg_volume=min_avg_volume,
        trailing_stop=False, fixed_stop_pct=stop_pct, spy_overlay=False,
    )
    if not pf.equity_curve or not pf.trades:
        return {"label": label, "status": "no_trades",
                "reason": f"{label} signals existed but produced zero filled trades"}

    start_date, end_date = pf.equity_curve[0]["date"], pf.equity_curve[-1]["date"]
    strat = metrics.equity_stats(pf.equity_curve, label)
    tstats = metrics.trade_stats(pf.trades)
    spy_curve = metrics.spy_buy_hold(store, start_date, end_date, start_equity)
    spy = metrics.equity_stats(spy_curve, "SPY buy & hold") if spy_curve else {}

    strat_rets = [(b["equity"] / a["equity"] - 1) for a, b in zip(pf.equity_curve[:-1], pf.equity_curve[1:])]
    spy_rets = [(b["equity"] / a["equity"] - 1) for a, b in zip(spy_curve[:-1], spy_curve[1:])] if spy_curve else []
    gate_report = None
    if len(strat_rets) == len(spy_rets) and len(strat_rets) >= 2:
        gate_report = run_gates(
            strat_daily_returns=strat_rets, spy_daily_returns=spy_rets,
            n_trades=len(pf.trades), is_return=None, oos_return=None,
        )

    png_path = os.path.join(out_dir, f"equity_curve_{key}.png")
    metrics.plot_equity(pf.equity_curve, spy_curve, png_path,
                        f"{label}: point-in-time backtest vs SPY ({start_date} -> {end_date})")

    report = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "strategy": key,
        "label": label,
        "status": "ok",
        "config": {
            "entry_rule": f"next-open after real-time {label} signal (fixed stop, no ATR trailing)",
            "stop_pct": stop_pct,
            "hold_days": hold_days,
            "slippage_bps": slippage_bps,
            "regime_gated": True,
            "min_price": min_price,
            "min_avg_volume": min_avg_volume,
            "signal_source": "backtest_harness/satellite_signals.py (offline replica of "
                              f"core.{key}_screener math over cached OHLCV bars)",
            "universe": universe,
            "n_signals": len(signals),
            "window": {"start": start_date, "end": end_date},
            "start_equity": start_equity,
        },
        "performance": strat,
        "spy_buy_hold": spy,
        "trade_stats": tstats,
        "p_value": round(gate_report.p_value, 4) if gate_report else None,
        "gates": gate_report.gates if gate_report else None,
        "trustworthy": gate_report.trustworthy if gate_report else None,
        "equity_curve": pf.equity_curve,
        "trades": pf.trades,
    }
    json_path = os.path.join(out_dir, f"{key}.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)
    if gate_report:
        print(gate_report.summary())
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-equity", type=float, default=100_000.0)
    ap.add_argument("--slippage-bps", type=float, default=10.0)
    args = ap.parse_args()

    from backtest_harness import data

    universe_symbols = sorted(f[:-5] for f in os.listdir(data.CACHE_DIR) if f.endswith(".json"))
    series = {s: data.load_cached(s) for s in universe_symbols}
    series = {s: b for s, b in series.items() if b}
    if "SPY" not in series:
        log.error("No cached SPY bars at backtest_harness/cache/SPY.json — aborting.")
        return 1
    store = data.BarStore(series)
    data.install_store(store)

    universe = build_universe(store)
    log.info("Universe: %d cached symbols (excl. index/sector ETFs): %s", len(universe), universe)

    today = datetime.date.today().isoformat()
    out_dir = os.path.join(REPO, "backtests", f"five_strategies_{today}")
    os.makedirs(out_dir, exist_ok=True)

    results: dict[str, dict] = {}
    for key in ("breakout", "meanrev", "earnmom"):
        try:
            results[key] = _run_satellite_strategy(
                key, store, universe, args.start_equity, args.slippage_bps, out_dir)
        except Exception as e:
            log.exception("%s failed", key)
            results[key] = {"label": STRATEGY_CONFIGS[key]["label"], "status": "error", "reason": str(e)}

    for key, bcfg in BLOCKED_STRATEGIES.items():
        results[key] = {"label": bcfg["label"], "status": "blocked_paid_tier", "reason": bcfg["reason"]}

    summary_path = os.path.join(out_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump({
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "methodology_note": (
                "Universe is the 30 non-ETF symbols present in the committed bar cache "
                "(backtest_harness/cache/*.json), not the full 103-symbol SP80_UNIVERSE "
                "production scans. Breakout, Mean Reversion and Earnings Momentum signals "
                "are generated point-in-time from that cache with no look-ahead "
                "(backtest_harness/satellite_signals.py); Earnings Momentum additionally "
                "uses yfinance earnings dates via backtest_harness/earnings_data.py "
                "(disk-cached). Insider Buying and Short Squeeze remain unbacktestable: "
                "FMP /stable/insider-trading and /stable/short-interest are paid-tier "
                "endpoints (402/403 on the free plan), not a network-egress issue."
            ),
            "results": results,
        }, f, indent=2)

    _print_summary_table(results)
    log.info("Summary written to %s", os.path.relpath(summary_path, REPO))
    return 0


def _print_summary_table(results: dict[str, dict]) -> None:
    line = "=" * 92
    print("\n" + line)
    print("  5-STRATEGY BACKTEST SUMMARY")
    print(line)
    header = f"  {'Strategy':<20}{'Status':<20}{'Sharpe':>8}{'Win%':>8}{'p-value':>10}{'Trades':>9}"
    print(header)
    print("  " + "-" * 88)
    for key in ("breakout", "meanrev", "earnmom", "insider", "squeeze"):
        r = results[key]
        label = r.get("label", key)
        status = r.get("status", "?")
        if status == "ok":
            perf = r.get("performance", {})
            tstats = r.get("trade_stats", {})
            sharpe = f"{perf.get('sharpe'):.2f}" if perf.get("sharpe") is not None else "n/a"
            winr = f"{tstats.get('win_rate_pct'):.1f}%" if tstats.get("win_rate_pct") is not None else "n/a"
            p = f"{r.get('p_value'):.4f}" if r.get("p_value") is not None else "n/a"
            n = tstats.get("num_trades", "n/a")
            print(f"  {label:<20}{'OK':<20}{sharpe:>8}{winr:>8}{p:>10}{str(n):>9}")
        else:
            print(f"  {label:<20}{status:<20}{'--':>8}{'--':>8}{'--':>10}{'--':>9}")
    print(line)
    for key in ("insider", "squeeze"):
        r = results[key]
        if str(r.get("status", "")).startswith("blocked"):
            print(f"  [{r['label']}] blocked: {r['reason']}")
    print(line + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
