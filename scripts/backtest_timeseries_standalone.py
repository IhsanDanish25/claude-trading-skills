#!/usr/bin/env python3
"""Standalone validation run for the time-series confirming filter
(core/timeseries_signal.py), reusing the same harness/gates as
backtest_5_strategies.py's breakout/meanrev/earnmom runs.

Scoped down from the full universe/window for tractability: ARIMA refits
cost ~1s each on cached daily bars, so a full universe (~440 symbols) x full
2020-2026 window x refit-every-5-days would run for hours. This uses a
reduced liquid-symbol universe and a shorter window as an initial, honest
signal-quality check -- NOT a substitute for scaling this to the full
standard window before TIMESERIES_ENABLED is ever considered.

Usage:
    python3 scripts/backtest_timeseries_standalone.py
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPOSITE_USE_FMP", "false")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest.timeseries_standalone")

REDUCED_UNIVERSE = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMD",
    "META",
    "GOOGL",
    "AMZN",
    "TSLA",
    "NFLX",
    "CRM",
    "ADBE",
    "JPM",
]
WINDOW_YEARS = 2


def main() -> int:
    from backtest_harness import data

    universe_symbols = data.list_cached_symbols()
    series = {s: data.load_cached(s) for s in universe_symbols}
    series = {s: b for s, b in series.items() if b}
    if "SPY" not in series:
        log.error("No cached SPY bars — aborting.")
        return 1
    store = data.BarStore(series)
    data.install_store(store)

    universe = [s for s in REDUCED_UNIVERSE if s in store.series]
    log.info(
        "Reduced universe: %d/%d requested symbols cached", len(universe), len(REDUCED_UNIVERSE)
    )

    cal = store.trading_calendar("SPY")
    full_start, full_end = cal[0], cal[-1]
    window_start = max(full_start, full_end - datetime.timedelta(days=365 * WINDOW_YEARS))
    log.info(
        "Window: %s -> %s (%d years, vs full cache %s -> %s)",
        window_start,
        full_end,
        WINDOW_YEARS,
        full_start,
        full_end,
    )

    # Monkeypatch the window into _run_satellite_strategy by temporarily
    # narrowing what trading_calendar('SPY') returns isn't clean, so instead
    # we reimplement the window selection here and call the signal generator
    # + simulation directly via the same helper, matching its internals.
    from backtest_harness import earnings_engine, metrics, satellite_signals
    from core import config as cfg
    from validation_gates import run_gates, total_return

    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backtests",
        f"timeseries_standalone_{datetime.date.today().isoformat()}",
    )
    os.makedirs(out_dir, exist_ok=True)

    signals = satellite_signals.get_historical_timeseries_signals(
        store,
        universe,
        window_start,
        full_end,
    )
    log.info(
        "timeseries: %d point-in-time signals across %d symbols (min_confidence=%.2f)",
        len(signals),
        len({s["symbol"] for s in signals}),
        cfg.TIMESERIES_MIN_CONFIDENCE,
    )

    result = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "scope": "REDUCED — not the full standard window/universe",
        "universe": universe,
        "window": {"start": window_start.isoformat(), "end": full_end.isoformat()},
        "min_confidence_threshold": cfg.TIMESERIES_MIN_CONFIDENCE,
        "n_signals": len(signals),
    }

    if not signals:
        result["status"] = "no_signals"
        result["reason"] = (
            f"model never reached the {cfg.TIMESERIES_MIN_CONFIDENCE:.0%} confidence "
            "threshold on this universe/window"
        )
        print(json.dumps(result, indent=2))
        with open(os.path.join(out_dir, "timeseries.json"), "w") as f:
            json.dump(result, f, indent=2)
        return 0

    pf = earnings_engine.run_earnings_simulation(
        store,
        signals,
        start_equity=100_000.0,
        slippage_bps=10.0,
        hold_days=cfg.TIMESERIES_HOLD_DAYS,
        regime_gated=True,
        window_start=window_start.isoformat(),
        window_end=full_end.isoformat(),
        min_surprise_pct=0.0,
        min_price=cfg.TIMESERIES_MIN_PRICE,
        min_avg_volume=cfg.TIMESERIES_MIN_AVG_VOLUME,
        trailing_stop=False,
        fixed_stop_pct=cfg.TIMESERIES_STOP_PCT,
        spy_overlay=False,
    )
    if not pf.equity_curve or not pf.trades:
        result["status"] = "no_trades"
        print(json.dumps(result, indent=2))
        with open(os.path.join(out_dir, "timeseries.json"), "w") as f:
            json.dump(result, f, indent=2)
        return 0

    start_date, end_date = pf.equity_curve[0]["date"], pf.equity_curve[-1]["date"]
    strat = metrics.equity_stats(pf.equity_curve, "Time-series (standalone, reduced scope)")
    tstats = metrics.trade_stats(pf.trades)
    spy_curve = metrics.spy_buy_hold(store, start_date, end_date, 100_000.0)
    spy = metrics.equity_stats(spy_curve, "SPY buy & hold") if spy_curve else {}

    strat_rets = [
        (b["equity"] / a["equity"] - 1) for a, b in zip(pf.equity_curve[:-1], pf.equity_curve[1:])
    ]
    spy_rets = (
        [(b["equity"] / a["equity"] - 1) for a, b in zip(spy_curve[:-1], spy_curve[1:])]
        if spy_curve
        else []
    )
    gate_report = None
    if len(strat_rets) == len(spy_rets) and len(strat_rets) >= 2:
        half = len(strat_rets) // 2
        is_return = total_return(strat_rets[:half])
        oos_return = total_return(strat_rets[half:])
        gate_report = run_gates(
            strat_daily_returns=strat_rets,
            spy_daily_returns=spy_rets,
            n_trades=len(pf.trades),
            is_return=is_return,
            oos_return=oos_return,
        )

    result.update(
        {
            "status": "ok",
            "performance": strat,
            "spy_buy_hold": spy,
            "trade_stats": tstats,
            "p_value": round(gate_report.p_value, 4) if gate_report else None,
            "overfit_ratio": (
                round(gate_report.overfit_ratio, 3)
                if gate_report and gate_report.overfit_ratio != float("inf")
                else None
            ),
            "gates": gate_report.gates if gate_report else None,
            "trustworthy": gate_report.trustworthy if gate_report else None,
            "beats_field": gate_report.beats_field if gate_report else None,
        }
    )
    with open(os.path.join(out_dir, "timeseries.json"), "w") as f:
        json.dump({**result, "equity_curve": pf.equity_curve, "trades": pf.trades}, f, indent=2)
    print(json.dumps(result, indent=2))
    if gate_report:
        print(gate_report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
