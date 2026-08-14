#!/usr/bin/env python3
"""
Buffett Value Sell Agent - exit decisions and order construction.

Exits fire only on: (1) price reaching/exceeding the fair-value target,
(2) a fundamentals-driven thesis break flagged by the hold agent, or
(3) a materially better margin-of-safety opportunity elsewhere. There is no
fixed holding period and no hard percentage stop-loss. Orders are simple
market/limit sells -- never bracket/OCO orders, since this strategy carries
no stop leg to attach.
"""

import logging
from typing import Any, Dict, List, Optional

from buy import MIN_PROFIT_FLOOR

log = logging.getLogger(__name__)


def check_profit_target_reached(current_price: float, fair_value_target: Optional[float]) -> bool:
    """True when price has reached/exceeded the fair-value (Graham number) target."""
    if not fair_value_target or fair_value_target <= 0:
        return False
    return current_price >= fair_value_target


def evaluate_exit(
    position: Dict[str, Any],
    monitor_result: Dict[str, Any],
    current_price: float,
) -> Optional[Dict[str, Any]]:
    """
    Decide whether one open position should be exited right now.

    Args:
        position: {symbol, qty, entry_price, entry_snapshot}
        monitor_result: hold.monitor_position/monitor_portfolio output for this symbol
        current_price: latest quote

    Returns:
        An exit decision dict if warranted, else None. Never triggers on
        time-in-trade -- only thesis break, profit target, or a materially
        better opportunity.
    """
    symbol = position["symbol"]
    entry_price = position.get("entry_price", 0.0)
    fair_value_target = (
        position.get("entry_snapshot", {})
        .get("valuation", {})
        .get("valuation_details", {})
        .get("graham_number")
    )

    profit_per_share = current_price - entry_price

    # 1. Profit target reached, subject to the $10 minimum profit floor
    if check_profit_target_reached(current_price, fair_value_target):
        if profit_per_share >= MIN_PROFIT_FLOOR:
            return _build_exit(
                position,
                current_price,
                "profit_target",
                f"Price ${current_price:.2f} reached fair value target "
                f"${fair_value_target:.2f} with ${profit_per_share:.2f}/share profit",
            )
        log.debug(
            f"{symbol}: fair value reached but profit ${profit_per_share:.2f} "
            f"< ${MIN_PROFIT_FLOOR:.2f} floor -- holding"
        )

    # 2. Fundamentals-driven thesis break
    if monitor_result.get("fundamentals_deteriorated"):
        reasons = "; ".join(monitor_result.get("deterioration_reasons", []))
        return _build_exit(position, current_price, "thesis_broken", reasons)

    # 3. Materially better opportunity elsewhere
    better = monitor_result.get("better_opportunity")
    if better:
        return _build_exit(
            position,
            current_price,
            "better_opportunity",
            f"{better['symbol']} offers higher conviction "
            f"({better.get('conviction_score', 0):.2f}) than the current position",
        )

    return None


def _build_exit(
    position: Dict[str, Any],
    current_price: float,
    reason_code: str,
    reason_detail: str,
) -> Dict[str, Any]:
    symbol = position["symbol"]
    qty = position.get("qty", 0)
    return {
        "symbol": symbol,
        "qty": qty,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "exit_price_est": current_price,
        "order": build_sell_order(symbol, qty, current_price),
    }


def build_sell_order(symbol: str, qty: float, current_price: float) -> Dict[str, Any]:
    """
    Build a simple (non-bracket) sell order.

    Buffett Value carries no stop-loss leg by design, so every exit is a
    single-leg limit sell -- never an Alpaca bracket/OCO order.
    """
    return {
        "symbol": symbol,
        "qty": qty,
        "side": "sell",
        "type": "limit",
        "limit_price": round(current_price * 0.999, 2),  # small buffer below quote to fill
        "time_in_force": "day",
        "order_class": "simple",
    }


def generate_sell_signals(
    positions: List[Dict[str, Any]],
    monitor_results: Dict[str, Dict[str, Any]],
    current_prices: Dict[str, float],
) -> List[Dict[str, Any]]:
    """
    Evaluate every open position and return sell decisions.

    Args:
        positions: open Buffett Value positions
        monitor_results: {symbol: monitor_result} from hold.monitor_portfolio
        current_prices: {symbol: latest_price}
    """
    exits = []
    for position in positions:
        symbol = position["symbol"]
        monitor_result = monitor_results.get(symbol, {})
        current_price = current_prices.get(symbol)
        if current_price is None:
            log.warning(f"{symbol}: no current price available, skipping exit evaluation")
            continue

        exit_decision = evaluate_exit(position, monitor_result, current_price)
        if exit_decision:
            exits.append(exit_decision)
            log.info(
                f"{symbol}: SELL signal ({exit_decision['reason_code']}) - "
                f"{exit_decision['reason_detail']}"
            )

    return exits


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Buffett Value Sell Agent")
    print("Run via pytest for real coverage: skills/buffett-value/scripts/tests/test_sell.py")
