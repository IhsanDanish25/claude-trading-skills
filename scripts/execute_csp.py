#!/usr/bin/env python3
"""
CSP ORDER EXECUTOR
Reads weekly_csp_order.json and places the CSP via Alpaca options.

Usage:
  python3 scripts/execute_csp.py                  # live execution
  python3 scripts/execute_csp.py --dry-run         # simulate only
  python3 scripts/execute_csp.py --check            # check status without trading
  python3 scripts/execute_csp.py --close            # close existing CSPs

Requires: options_trading_level >= 1 on account
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import logger, config
from core.broker import BrokerClient

log = logger.setup("execute_csp")
STATE_FILE = os.path.join(config.STATE_DIR, "weekly_csp_order.json")


def load_order() -> dict | None:
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def execute_csp(broker: BrokerClient, order: dict, dry_run: bool = False) -> dict:
    pick = order.get("pick", {})

    symbol     = pick.get("symbol")
    strike     = pick.get("strike")
    premium    = pick.get("premium")
    expiration = next_week_friday()

    if not all([symbol, strike]):
        log.error(f"Invalid pick: {pick}")
        return {"blocked": True, "reason": "missing_fields"}

    log.info(f"Executing CSP: {symbol} ${strike} put exp {expiration}")
    log.info(f"  Est. premium: ${premium:.2f}" if premium else "  Premium: MARKET")

    if dry_run:
        log.info("  DRY RUN — no order placed")
        return {"dry_run": True, "symbol": symbol, "strike": strike, "expiration": expiration}

    opt_level = broker.options_level()
    log.info(f"  Options level: {opt_level}")
    if opt_level < 1:
        log.error(f"  OPTIONS NOT APPROVED (level={opt_level})")
        return {"blocked": True, "reason": f"options_level={opt_level}_not_approved"}

    acct = broker.get_account()
    cash = float(acct.cash)
    log.info(f"  Cash: ${cash:.2f}")
    log.info(f"  Portfolio: ${float(acct.portfolio_value):.2f}")

    if cash < strike * 100:
        log.warning(f"  Insufficient cash for full CSP: need ${strike * 100:.0f}, have ${cash:.2f}")
        return {"blocked": True, "reason": "insufficient_cash"}

    result = broker.sell_csp(
        symbol=symbol,
        strike=strike,
        expiration=expiration,
        premium=premium,
        qty=1,
    )

    # Save execution result
    result["executed_at"] = datetime.now().isoformat()
    result["pick"] = pick

    result_file = os.path.join(config.STATE_DIR, f"csp_executed_{symbol}_{expiration}.json")
    with open(result_file, "w") as f:
        json.dump(result, f, indent=2)
    log.info(f"  Result → {result_file}")

    return result


def next_week_friday() -> str:
    today = datetime.now()
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    next_fri = today + timedelta(days=days_ahead + 0)
    return next_fri.strftime("%Y-%m-%d")


def check_status(broker: BrokerClient):
    log.info("Checking open option positions...")
    try:
        from alpaca.trading.requests import GetOptionContractsRequest, GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        orders = broker.trade.get_option_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )
        log.info(f"Open option orders: {len(orders)}")
        for o in orders:
            log.info(f"  {o.symbol} | side={o.side} | qty={o.qty} | status={o.status}")

        pos = broker.get_options_positions()
        log.info(f"Option positions: {len(pos)}")
        if pos:
            for p in pos:
                log.info(f"  {p.symbol} | qty={p.qty} | mv=${float(p.market_value):.2f}")

        acct = broker.get_account()
        log.info(f"Options BP: ${float(acct.options_buying_power):.2f}")
        log.info(f"Options level: {broker.options_level()}")

    except Exception as e:
        log.error(f"Status check failed: {e}")


def close_all(broker: BrokerClient):
    log.info("Closing ALL option positions...")
    try:
        from alpaca.trading.enums import OrderSide
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        orders = broker.trade.get_option_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )
        for o in orders:
            if "sell" in o.side.value.lower():
                log.info(f"  Buying to close {o.symbol}")
                broker.close_option(o.symbol)

    except Exception as e:
        log.error(f"Close all failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="CSP Order Executor")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--close", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    config.validate()
    broker = BrokerClient()

    if args.check:
        check_status(broker)
        return

    if args.close:
        close_all(broker)
        return

    order = load_order()

    if not order:
        log.error(f"No order file at {STATE_FILE}")
        log.info("Run weekly_csp.py first to generate the order.")
        return

    status = order.get("status", "unknown")
    pick   = order.get("pick", {})

    log.info(f"Order status: {status}")
    log.info(f"Pick: {pick.get('symbol')} ${pick.get('strike')} CSP")

    if status != "READY_TO_EXECUTE" and not args.force:
        log.warning(f"Not ready to execute (status={status}). Use --force to override.")
        return

    result = execute_csp(broker, order, dry_run=args.dry_run)

    if result.get("blocked"):
        log.error(f"CSP BLOCKED: {result.get('reason')}")
        sys.exit(1)
    elif result.get("dry_run"):
        log.info("Dry run complete.")
    else:
        prem = result.get("premium_collected", 0)
        log.info(f"✅ CSP EXECUTED: premium=${prem:.2f}")


if __name__ == "__main__":
    main()