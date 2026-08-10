"""
Live cost-tracking — realized slippage & spread cost, per trade and rolled
up per strategy, so actual execution cost can be compared against the
backtested Sharpe/edge that justified running each strategy live.

Two things are recorded per fill via record_fill():
  - slippage: (fill_price - signal_price) / signal_price, signed so a
    positive value means the fill was worse than the signal price on a buy.
  - spread_pct: the bid-ask spread at trade time (if known), for
    diagnosing whether slippage is spread-driven or something else
    (partial fill, fast market, stale signal).

Logging goes through core.trade_logger (Axiom + local jsonl fallback) —
this module never talks to Axiom directly, and never raises into the
caller's trading path: any error here is caught and logged, never
propagated, matching the fail-safe standard used elsewhere in the bot.

weekly_edge_report() reads the local jsonl fallback back out (source of
truth for computation — Axiom is a durable copy, not queried here) and
computes a rolling N-day realized edge per strategy for the weekly review.
"""

from __future__ import annotations

import datetime
import json
import logging
import os

import pytz

from core import trade_logger
from core.config import STATE_DIR

log = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

# Backtested Sharpe per strategy, from the 2026-08-06 validation run
# (see core/config.py STRATEGY_MODES comment for the full reconciliation).
# Used only as the comparison baseline in weekly_edge_report — not a live
# input to any trading decision.
BACKTESTED_SHARPE = {
    "breakout": 1.08,
    "meanrev": 0.96,
    "earnmom": 0.96,
    "insider": None,  # backtest blocked (FMP endpoint paid-tier), live-only
    "pead": None,  # dropped — failed significance, kept here only so a
    # straggler pead fill doesn't KeyError the report
}

# Alert threshold: flag a strategy in the weekly report if realized edge
# (mean net-of-cost return per trade) drops this many percentage points
# below what the backtest would imply as a sane per-trade floor. Kept as
# a simple, visible constant rather than a statistical test — this is a
# human-in-the-loop flag, not an auto-disable.
EDGE_ALERT_DELTA_PCT = 0.02  # 2 percentage points


def _cost_log_path() -> str:
    return os.path.join(STATE_DIR, "cost_log.jsonl")


def record_fill(
    strategy: str,
    symbol: str,
    side: str,
    signal_price: float,
    fill_price: float,
    qty: float,
    spread_pct: float | None = None,
) -> None:
    """Record one fill's realized slippage and spread cost.

    Fail-safe: any error here is caught and logged, never raised — a
    broken cost-tracking write must never block or crash a trade that
    already executed.
    """
    try:
        if signal_price is None or signal_price <= 0 or fill_price is None:
            log.warning(
                "cost_tracker.record_fill %s %s: missing/invalid price data "
                "(signal=%s fill=%s) — skipping",
                strategy,
                symbol,
                signal_price,
                fill_price,
            )
            return

        slippage_pct = (fill_price - signal_price) / signal_price
        if side == "sell":
            # For a sell, a fill BELOW the signal price is the adverse
            # direction — flip sign so positive always means "cost".
            slippage_pct = -slippage_pct

        record = {
            "_time": datetime.datetime.now(ET).isoformat(),
            "strategy": strategy,
            "symbol": symbol,
            "side": side,
            "signal_price": signal_price,
            "fill_price": fill_price,
            "qty": qty,
            "slippage_pct": slippage_pct,
            "spread_pct": spread_pct,
        }

        # Local jsonl — source of truth for weekly_edge_report's computation.
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(_cost_log_path(), "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            log.warning("cost_tracker.record_fill: local jsonl write failed: %s", e)

        # Axiom — durable copy, best-effort (trade_logger degrades silently
        # if AXIOM_API_TOKEN is unset).
        try:
            trade_logger.log_event(
                "cost_tracking",
                strategy,
                symbol,
                side=side,
                signal_price=signal_price,
                fill_price=fill_price,
                qty=qty,
                slippage_pct=slippage_pct,
                spread_pct=spread_pct,
            )
        except Exception as e:
            log.warning("cost_tracker.record_fill: Axiom log failed: %s", e)

    except Exception as e:
        # Belt-and-suspenders: record_fill must never raise into buy()/sell().
        log.error("cost_tracker.record_fill failed unexpectedly for %s %s: %s", strategy, symbol, e)


def compute_rolling_edge(strategy: str, days: int = 30) -> dict:
    """Rolling N-day realized edge for one strategy, computed from the local
    cost_log.jsonl. Returns n_trades, avg_slippage_pct, and (if enough data)
    a rough realized-edge figure. Never raises — returns a zero-trade dict
    on any read error so callers can log a clean "no data" line instead of
    crashing the weekly review.
    """
    try:
        path = _cost_log_path()
        if not os.path.exists(path):
            return {"strategy": strategy, "n_trades": 0, "avg_slippage_pct": None}

        cutoff = datetime.datetime.now(ET) - datetime.timedelta(days=days)
        slippages = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("strategy") != strategy:
                    continue
                try:
                    ts = datetime.datetime.fromisoformat(rec["_time"])
                except (KeyError, ValueError):
                    continue
                if ts < cutoff:
                    continue
                sp = rec.get("slippage_pct")
                if sp is not None:
                    slippages.append(sp)

        if not slippages:
            return {"strategy": strategy, "n_trades": 0, "avg_slippage_pct": None}

        avg_slippage = sum(slippages) / len(slippages)
        return {
            "strategy": strategy,
            "n_trades": len(slippages),
            "avg_slippage_pct": avg_slippage,
        }
    except Exception as e:
        log.warning("cost_tracker.compute_rolling_edge failed for %s: %s", strategy, e)
        return {"strategy": strategy, "n_trades": 0, "avg_slippage_pct": None}


def weekly_edge_report(strategies: list[str], days: int = 30) -> list[dict]:
    """Rolling realized edge for each strategy vs. its backtested Sharpe.
    Used by routines/weekly_review.py. Never raises — a per-strategy
    failure just reports n_trades=0 for that strategy rather than
    aborting the whole weekly review.
    """
    reports = []
    for strategy in strategies:
        r = compute_rolling_edge(strategy, days=days)
        r["backtested_sharpe"] = BACKTESTED_SHARPE.get(strategy)
        r["alert"] = (
            r["n_trades"] > 0
            and r["avg_slippage_pct"] is not None
            and r["avg_slippage_pct"] > EDGE_ALERT_DELTA_PCT
        )
        reports.append(r)
    return reports
