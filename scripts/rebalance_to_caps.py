#!/usr/bin/env python3
"""
One-shot rebalance: close excess positions and trim survivors to the
per-position cap.  Dry-run by default — pass --execute to place real orders.

Usage:
    python scripts/rebalance_to_caps.py --target-positions 2
    python scripts/rebalance_to_caps.py --target-positions 3 --execute
"""
from __future__ import annotations

import argparse
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import MAX_POSITION_SIZE_PCT, MAX_OPEN_POSITIONS
from core.broker import BrokerClient


def parse_args():
    p = argparse.ArgumentParser(description="Rebalance positions to sizing caps")
    p.add_argument(
        "--target-positions", type=int, default=None,
        help=f"Target number of positions to keep (default: MAX_OPEN_POSITIONS={MAX_OPEN_POSITIONS})",
    )
    p.add_argument(
        "--max-pct", type=float, default=None,
        help=f"Per-position cap as decimal (default: MAX_POSITION_SIZE_PCT={MAX_POSITION_SIZE_PCT})",
    )
    p.add_argument(
        "--keep", nargs="*", default=None,
        help="Symbols to prioritize keeping (e.g. --keep PANW AAPL)",
    )
    p.add_argument(
        "--execute", action="store_true",
        help="Actually place sell orders (default is dry-run preview only)",
    )
    return p.parse_args()


def run():
    args = parse_args()
    target_n = args.target_positions or MAX_OPEN_POSITIONS
    cap_pct = args.max_pct or MAX_POSITION_SIZE_PCT
    keep_set = {s.upper() for s in (args.keep or [])}

    broker = BrokerClient()
    equity = broker.portfolio_value()
    cap_dollars = equity * cap_pct
    positions = broker.get_positions()

    if not positions:
        print("No open positions — nothing to do.")
        return

    rows = []
    for p in positions:
        mv = abs(float(p.market_value or 0))
        entry = float(p.avg_entry_price)
        cur = float(p.current_price) if hasattr(p, "current_price") and p.current_price else entry
        qty = int(float(p.qty))
        pnl = float(p.unrealized_pl or 0)
        pct_equity = mv / equity * 100 if equity > 0 else 0
        rows.append({
            "symbol": p.symbol,
            "qty": qty,
            "entry": entry,
            "price": cur,
            "market_value": mv,
            "pct_equity": pct_equity,
            "pnl": pnl,
            "over_cap": mv > cap_dollars,
        })

    rows.sort(key=lambda r: r["market_value"])

    # Decide which to keep: prioritize --keep symbols, then smallest positions
    if keep_set:
        kept = [r for r in rows if r["symbol"] in keep_set]
        rest = [r for r in rows if r["symbol"] not in keep_set]
        rest.sort(key=lambda r: r["market_value"])
        kept.extend(rest)
        ordered = kept
    else:
        ordered = list(rows)

    to_keep = ordered[:target_n]
    to_close = ordered[target_n:]
    keep_symbols = {r["symbol"] for r in to_keep}

    total_deployed = sum(r["market_value"] for r in rows)

    # Print current state
    print(f"\n{'='*72}")
    print(f"  REBALANCE PLAN {'(DRY RUN)' if not args.execute else '*** LIVE ***'}")
    print(f"{'='*72}")
    print(f"  Equity:           ${equity:>12,.2f}")
    print(f"  Deployed:         ${total_deployed:>12,.2f}  ({total_deployed/equity*100:.1f}%)")
    print(f"  Per-position cap: ${cap_dollars:>12,.2f}  ({cap_pct*100:.1f}%)")
    print(f"  Positions:        {len(rows)} → {target_n}")
    print()

    # Actions table
    hdr = f"  {'Action':<8} {'Symbol':<6} {'Qty':>5} {'Price':>9} {'Mkt Value':>11} {'% Eq':>6} {'P&L':>9} {'Sell Qty':>9} {'Sell $':>10}"
    print(hdr)
    print(f"  {'-'*len(hdr)}")

    sell_plan = []

    for r in rows:
        sym = r["symbol"]
        if sym in keep_symbols:
            if r["over_cap"]:
                trim_to_qty = max(1, int(cap_dollars / r["price"])) if r["price"] > 0 else r["qty"]
                sell_qty = r["qty"] - trim_to_qty
                if sell_qty > 0:
                    sell_val = sell_qty * r["price"]
                    action = "TRIM"
                    sell_plan.append({"symbol": sym, "qty": sell_qty, "action": "trim"})
                else:
                    action = "KEEP"
                    sell_qty = 0
                    sell_val = 0
            else:
                action = "KEEP"
                sell_qty = 0
                sell_val = 0
        else:
            action = "CLOSE"
            sell_qty = r["qty"]
            sell_val = r["market_value"]
            sell_plan.append({"symbol": sym, "qty": sell_qty, "action": "close"})

        flag = " <<<" if action != "KEEP" else ""
        print(f"  {action:<8} {sym:<6} {r['qty']:>5} ${r['price']:>8,.2f} ${r['market_value']:>10,.2f} {r['pct_equity']:>5.1f}% ${r['pnl']:>+8,.0f} {sell_qty:>9} ${sell_val:>9,.0f}{flag}")

    total_sell = sum(s["qty"] * next(r["price"] for r in rows if r["symbol"] == s["symbol"]) for s in sell_plan)
    remaining = total_deployed - total_sell
    print()
    print(f"  Total to sell:    ${total_sell:>12,.0f}")
    print(f"  Remaining deploy: ${remaining:>12,.0f}  ({remaining/equity*100:.1f}%)")
    print(f"  Freed to cash:    ${total_sell:>12,.0f}")
    print()

    if not sell_plan:
        print("  Nothing to do — already within caps.")
        return

    if not args.execute:
        print("  *** DRY RUN — no orders placed ***")
        print("  Re-run with --execute to place sell orders.")
        print()
        return

    # Execute sells
    print("  Executing sells...")
    for s in sell_plan:
        sym, qty, act = s["symbol"], s["qty"], s["action"]
        label = "Closing" if act == "close" else "Trimming"
        try:
            if act == "close":
                broker.close_position(sym)
            else:
                broker.sell(sym, qty=qty)
            print(f"    ✓ {label} {sym}: sold {qty} shares")
            time.sleep(0.3)
        except Exception as e:
            print(f"    ✗ {label} {sym} FAILED: {e}")

    print()
    time.sleep(2)
    new_positions = broker.get_positions()
    new_equity = broker.portfolio_value()
    new_deployed = sum(abs(float(p.market_value or 0)) for p in new_positions)
    print(f"  After rebalance:")
    print(f"    Equity:    ${new_equity:>12,.2f}")
    print(f"    Deployed:  ${new_deployed:>12,.2f}  ({new_deployed/new_equity*100:.1f}%)")
    print(f"    Positions: {len(new_positions)}")
    for p in new_positions:
        mv = abs(float(p.market_value or 0))
        print(f"      {p.symbol:<6} {int(float(p.qty)):>5} sh  ${mv:>10,.2f}  ({mv/new_equity*100:.1f}%)")
    print()


if __name__ == "__main__":
    run()
