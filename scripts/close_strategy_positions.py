#!/usr/bin/env python3
"""Find and cleanly close open positions tagged with retired strategies.

Cross-references core.pead_tracker's per-symbol strategy tags against live
Alpaca positions to find open positions still held under strategies being
dropped from STRATEGY_MODE (default: breakout, earnmom). Before closing a
position, cancels any stale open sell orders for that symbol first, reusing
the same cancel-first safety pattern as core.safe_oco_attach -- otherwise a
leftover OCO stop/target order can hold the shares and reject the close.

Also flags:
  - stale tracker entries (tracked, but the broker shows no open position --
    tracker just needs cleanup, nothing to trade)
  - untracked open positions (open on the broker but missing a tracker entry
    entirely, e.g. state lost across a redeploy without a persistent volume
    for STATE_DIR) -- strategy can't be attributed, so these are surfaced for
    manual review rather than acted on automatically

Dry-run by default -- shows the plan without touching orders or state.
Pass --execute to actually cancel stale orders, close positions, and untrack them.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.broker import BrokerClient
from core.pead_tracker import get_all as tracker_get_all
from core.pead_tracker import remove_position
from core.safe_oco_attach import cancel_open_sell_orders

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_STRATEGIES = ["breakout", "earnmom"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strategies", nargs="+", default=DEFAULT_STRATEGIES, metavar="NAME",
                   help=f"Strategy tags to close out (default: {' '.join(DEFAULT_STRATEGIES)})")
    p.add_argument("--execute", action="store_true",
                   help="Actually cancel stale orders and close positions (default: dry-run)")
    return p.parse_args()


def build_plan(broker: BrokerClient, tracker: dict, strategies: list[str]) -> dict:
    """Cross-reference tracked positions for *strategies* against live Alpaca
    positions. Returns three buckets: to_close, stale_tracker, untracked_positions."""
    strategy_set = {s.strip().lower() for s in strategies}
    live = {p.symbol: p for p in broker.get_positions()}
    tracked_symbols = set(tracker.keys())

    to_close = []
    stale_tracker = []
    for sym, info in tracker.items():
        if info.get("strategy", "").lower() not in strategy_set:
            continue
        if sym in live:
            pos = live[sym]
            to_close.append({
                "symbol": sym,
                "strategy": info.get("strategy"),
                "qty": float(pos.qty),
                "market_value": float(pos.market_value or 0),
                "unrealized_pl": float(pos.unrealized_pl or 0),
                "entry_date": info.get("entry_date"),
            })
        else:
            stale_tracker.append({"symbol": sym, "strategy": info.get("strategy")})

    untracked_positions = [
        {"symbol": sym, "qty": float(p.qty), "market_value": float(p.market_value or 0)}
        for sym, p in live.items() if sym not in tracked_symbols
    ]

    return {
        "to_close": to_close,
        "stale_tracker": stale_tracker,
        "untracked_positions": untracked_positions,
    }


def format_plan(plan: dict, strategies: list[str]) -> list[str]:
    """Return the plan as a list of log-friendly lines (no trailing newlines)."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"STRATEGY CLOSE-OUT PLAN — {', '.join(strategies)}")
    lines.append("=" * 60)

    if plan["to_close"]:
        lines.append(f"-- CLOSE ({len(plan['to_close'])} tracked, still open) --")
        for r in plan["to_close"]:
            lines.append(
                f"  {r['symbol']:6s}  [{r['strategy']}]  {r['qty']:>8.2f} sh  "
                f"${r['market_value']:>10,.2f}  P&L ${r['unrealized_pl']:>+8,.2f}  "
                f"since {r['entry_date']}")
    else:
        lines.append(f"-- No open positions tracked under {', '.join(strategies)} --")

    if plan["stale_tracker"]:
        lines.append("")
        lines.append(f"-- STALE TRACKER ENTRIES ({len(plan['stale_tracker'])}, already closed on broker) --")
        for r in plan["stale_tracker"]:
            lines.append(f"  {r['symbol']:6s}  [{r['strategy']}]  -- removing from tracker only")

    if plan["untracked_positions"]:
        lines.append("")
        lines.append(f"-- WARNING: {len(plan['untracked_positions'])} open position(s) with NO tracker entry --")
        lines.append("   Strategy can't be attributed (state lost?) -- review manually:")
        for r in plan["untracked_positions"]:
            lines.append(f"  {r['symbol']:6s}  {r['qty']:>8.2f} sh  ${r['market_value']:>10,.2f}")

    lines.append("=" * 60)
    return lines


def print_plan(plan: dict, strategies: list[str]):
    for line in format_plan(plan, strategies):
        print(line)


def execute_plan(broker: BrokerClient, plan: dict,
                 remove_fn=remove_position,
                 logger: logging.Logger | None = None) -> bool:
    """Execute the close-out plan. Returns True if all closes succeeded.

    If *logger* is provided, uses it instead of print() for output. *remove_fn*
    is injectable so callers (and tests) can avoid touching the real tracker file.
    """
    def _out(msg: str):
        if logger:
            logger.info(msg)
        else:
            print(msg)

    ok = True

    for r in plan["stale_tracker"]:
        remove_fn(r["symbol"])
        _out(f"  UNTRACKED (stale) {r['symbol']:6s}  [{r['strategy']}]")

    for r in plan["to_close"]:
        sym = r["symbol"]
        try:
            cancelled = cancel_open_sell_orders(broker, sym)
            if cancelled:
                _out(f"  {sym:6s}  cancelled {cancelled} stale sell order(s) first")
            broker.close_position(sym)
            remove_fn(sym)
            _out(f"  CLOSED {sym:6s}  [{r['strategy']}]  {r['qty']:.2f} sh")
        except Exception as e:
            _out(f"  FAILED to close {sym}: {e}")
            ok = False

    if plan["untracked_positions"]:
        _out(f"  Skipped {len(plan['untracked_positions'])} untracked position(s) -- review manually.")

    status = "All closes submitted." if ok else "Some closes FAILED -- check above."
    _out(f"  {status}")
    return ok


def main():
    args = parse_args()

    print("Connecting to Alpaca...")
    broker = BrokerClient()

    market_open = broker.is_market_open()
    if not market_open:
        print("⏸  Market is CLOSED — close orders will queue for next open.")

    tracker = tracker_get_all()
    plan = build_plan(broker, tracker, args.strategies)
    print_plan(plan, args.strategies)

    if not plan["to_close"] and not plan["stale_tracker"]:
        return

    if not args.execute:
        print("This is a DRY RUN. Pass --execute to cancel stale orders and close positions.\n")
        return

    execute_plan(broker, plan)


if __name__ == "__main__":
    main()
