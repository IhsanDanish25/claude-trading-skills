#!/usr/bin/env python3
"""Rebalance an Alpaca portfolio to position-count and position-size caps.

Dry-run by default — shows the plan without touching orders.
Pass --execute to submit market sell orders.

Ranking: positions are ranked by unrealized P&L percentage (best kept).
Override with --keep SYMBOL1 SYMBOL2 to choose survivors manually.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.broker import BrokerClient
from core.config import MAX_POSITION_SIZE_PCT, MAX_OPEN_POSITIONS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target-positions", type=int, default=MAX_OPEN_POSITIONS,
                   help=f"Max positions to keep (default: {MAX_OPEN_POSITIONS})")
    p.add_argument("--max-pct", type=float, default=MAX_POSITION_SIZE_PCT * 100,
                   help=f"Max per-position %% of equity (default: {MAX_POSITION_SIZE_PCT*100:.1f})")
    p.add_argument("--keep", nargs="+", metavar="SYM",
                   help="Manually choose which symbols to keep (overrides ranking)")
    p.add_argument("--execute", action="store_true",
                   help="Actually submit sell orders (default: dry-run)")
    return p.parse_args()


def build_plan(broker: BrokerClient, target_positions: int,
               max_pct: float, keep_symbols: list[str] | None):
    positions = broker.get_positions()
    equity = broker.portfolio_value()
    cash = broker.cash()

    if not positions:
        return {"status": "empty", "message": "No open positions.", "equity": equity}

    rows = []
    for p in positions:
        mv = abs(float(p.market_value or 0))
        qty = float(p.qty)
        upl = float(p.unrealized_pl or 0)
        cost = float(p.avg_entry_price or 0)
        pct_of_equity = (mv / equity * 100) if equity else 0
        upl_pct = (upl / (cost * qty) * 100) if (cost and qty) else 0
        rows.append({
            "symbol": p.symbol,
            "qty": qty,
            "avg_entry": cost,
            "market_value": mv,
            "unrealized_pl": upl,
            "unrealized_pl_pct": upl_pct,
            "pct_of_equity": pct_of_equity,
        })

    rows.sort(key=lambda r: r["unrealized_pl_pct"], reverse=True)

    if keep_symbols:
        keep_set = {s.upper() for s in keep_symbols}
        unknown = keep_set - {r["symbol"] for r in rows}
        if unknown:
            raise ValueError(f"--keep symbols not in portfolio: {unknown}")
        survivors = [r for r in rows if r["symbol"] in keep_set]
        closures = [r for r in rows if r["symbol"] not in keep_set]
    else:
        survivors = rows[:target_positions]
        closures = rows[target_positions:]

    trims = []
    max_frac = max_pct / 100
    for r in survivors:
        max_dollars = equity * max_frac
        if r["market_value"] > max_dollars:
            price_per_share = r["market_value"] / r["qty"] if r["qty"] else 0
            excess_dollars = r["market_value"] - max_dollars
            trim_shares = int(excess_dollars / price_per_share) if price_per_share else 0
            if trim_shares > 0:
                trims.append({**r, "trim_shares": trim_shares,
                              "post_trim_value": r["market_value"] - trim_shares * price_per_share,
                              "post_trim_pct": (r["market_value"] - trim_shares * price_per_share) / equity * 100})

    total_close_value = sum(r["market_value"] for r in closures)
    total_trim_value = sum(t["trim_shares"] * (t["market_value"] / t["qty"]) for t in trims)
    freed_cash = total_close_value + total_trim_value
    post_cash = cash + freed_cash
    survivor_value = sum(r["market_value"] for r in survivors) - total_trim_value
    post_deployed_pct = (survivor_value / equity * 100) if equity else 0

    within_caps = len(rows) <= target_positions and not trims
    if within_caps:
        return {
            "status": "within_caps",
            "message": "Already within caps. No action needed.",
            "equity": equity, "cash": cash, "positions": rows,
            "position_count": len(rows), "target_positions": target_positions,
            "deployed_pct": sum(r["pct_of_equity"] for r in rows),
        }

    return {
        "status": "needs_rebalance",
        "equity": equity,
        "cash": cash,
        "position_count": len(rows),
        "target_positions": target_positions,
        "max_pct": max_pct,
        "all_positions": rows,
        "survivors": survivors,
        "closures": closures,
        "trims": trims,
        "freed_cash": freed_cash,
        "post_cash": post_cash,
        "post_deployed_pct": post_deployed_pct,
    }


def print_plan(plan: dict):
    if plan["status"] == "empty":
        print(f"\n{'='*60}")
        print("REBALANCE PLAN — No open positions")
        print(f"Equity: ${plan['equity']:,.2f}")
        print(f"{'='*60}\n")
        return

    if plan["status"] == "within_caps":
        print(f"\n{'='*60}")
        print("REBALANCE CHECK — Already within caps ✓")
        print(f"{'='*60}")
        print(f"  Equity:     ${plan['equity']:,.2f}")
        print(f"  Cash:       ${plan['cash']:,.2f}")
        print(f"  Positions:  {plan['position_count']} / {plan['target_positions']} cap")
        print(f"  Deployed:   {plan['deployed_pct']:.1f}%")
        for r in plan["positions"]:
            flag = " ⚠ OVER CAP" if r["pct_of_equity"] > (plan.get("max_pct", MAX_POSITION_SIZE_PCT * 100)) else ""
            print(f"    {r['symbol']:6s}  {r['qty']:>8.2f} sh  "
                  f"${r['market_value']:>10,.2f}  ({r['pct_of_equity']:5.1f}%)  "
                  f"P&L ${r['unrealized_pl']:>+8,.2f} ({r['unrealized_pl_pct']:>+.1f}%){flag}")
        print(f"{'='*60}\n")
        return

    print(f"\n{'='*60}")
    print("REBALANCE PLAN (DRY RUN)")
    print(f"{'='*60}")
    print(f"  Equity:          ${plan['equity']:,.2f}")
    print(f"  Cash:            ${plan['cash']:,.2f}")
    print(f"  Positions:       {plan['position_count']} → {plan['target_positions']}")
    print(f"  Per-position cap: {plan['max_pct']:.1f}%")
    print()

    print("── CURRENT POSITIONS (ranked by P&L %) ──")
    for i, r in enumerate(plan["all_positions"], 1):
        kept = r["symbol"] in {s["symbol"] for s in plan["survivors"]}
        tag = "KEEP" if kept else "CLOSE"
        print(f"  {i}. [{tag:5s}] {r['symbol']:6s}  {r['qty']:>8.2f} sh  "
              f"${r['market_value']:>10,.2f}  ({r['pct_of_equity']:5.1f}%)  "
              f"P&L ${r['unrealized_pl']:>+8,.2f} ({r['unrealized_pl_pct']:>+.1f}%)")
    print()

    if plan["closures"]:
        print("── CLOSE (full liquidation) ──")
        for r in plan["closures"]:
            loss_flag = " ⚠ LOCKING IN LOSS" if r["unrealized_pl"] < -50 else ""
            print(f"  SELL ALL  {r['symbol']:6s}  {r['qty']:>8.2f} sh  "
                  f"≈ ${r['market_value']:>10,.2f}  "
                  f"P&L ${r['unrealized_pl']:>+8,.2f}{loss_flag}")
        print()

    if plan["trims"]:
        print("── TRIM (reduce to cap) ──")
        for t in plan["trims"]:
            print(f"  SELL {t['trim_shares']:>4d}  {t['symbol']:6s}  "
                  f"${t['market_value']:>10,.2f} → ${t['post_trim_value']:>10,.2f}  "
                  f"({t['pct_of_equity']:.1f}% → {t['post_trim_pct']:.1f}%)")
        print()

    print("── RESULT ──")
    print(f"  Freed cash:    ${plan['freed_cash']:>10,.2f}")
    print(f"  Post cash:     ${plan['post_cash']:>10,.2f}")
    print(f"  Post deployed: {plan['post_deployed_pct']:.1f}%")
    print(f"  Positions:     {plan['target_positions']}")
    print(f"{'='*60}\n")


def execute_plan(broker: BrokerClient, plan: dict):
    if plan["status"] != "needs_rebalance":
        print("Nothing to execute.")
        return True

    print("\n── EXECUTING ──")
    ok = True

    broker.cancel_all_orders()
    print("  Cancelled all open orders first.")

    for r in plan["closures"]:
        try:
            result = broker.sell(r["symbol"])
            oid = str(result.get("order", {}).id)[:8] if result.get("order") else "n/a"
            print(f"  CLOSED {r['symbol']:6s}  {r['qty']:.2f} sh  [order {oid}]")
        except Exception as e:
            print(f"  FAILED to close {r['symbol']}: {e}")
            ok = False

    for t in plan["trims"]:
        try:
            result = broker.sell(t["symbol"], qty=t["trim_shares"])
            oid = str(result.get("order", {}).id)[:8] if result.get("order") else "n/a"
            print(f"  TRIMMED {t['symbol']:6s}  -{t['trim_shares']} sh  [order {oid}]")
        except Exception as e:
            print(f"  FAILED to trim {t['symbol']}: {e}")
            ok = False

    status = "All orders submitted." if ok else "Some orders FAILED — check above."
    print(f"\n  {status}")
    return ok


def main():
    args = parse_args()
    max_frac = args.max_pct / 100

    print("Connecting to Alpaca...")
    broker = BrokerClient()

    market_open = broker.is_market_open()
    if not market_open:
        print("⏸  Market is CLOSED — sell orders will queue for next open.")

    plan = build_plan(broker, args.target_positions, args.max_pct, args.keep)
    print_plan(plan)

    if plan["status"] != "needs_rebalance":
        return

    if not args.execute:
        print("This is a DRY RUN. Pass --execute to submit orders.\n")
        return

    execute_plan(broker, plan)


if __name__ == "__main__":
    main()
