"""
E4 Portable Alpha: SPY Base
────────────────────────────
Parks idle cash in SPY as a beta base. When PEAD entries need capital,
sells enough SPY to fund them. Rebalances excess cash back into SPY
during midday/close if no PEAD trades are pending.

Called by:
  - market_open (pre-PEAD): sell SPY if needed for PEAD entries
  - midday_review: rebalance excess cash → SPY
  - market_close: log SPY base status
"""
from __future__ import annotations

import logging
from core import config
from core.notifier import send_trade_alert
from core.broker import BrokerClient

log = logging.getLogger(__name__)


def is_base_symbol(symbol: str) -> bool:
    """True when symbol is the SPY base holding managed by this module.
    Trade-protection OCOs must never be attached to it: they hold the full
    share qty, so every base rebalance sell is rejected by Alpaca with
    40310000 insufficient qty available."""
    return config.SPY_BASE_ENABLED and symbol.upper() == "SPY"


def get_spy_position(broker: BrokerClient) -> dict:
    """Return SPY position details or empty dict."""
    pos = broker.get_position("SPY")
    if not pos:
        return {"qty": 0, "value": 0.0, "avg_entry": 0.0}
    return {
        "qty": int(float(pos.qty)),
        "value": abs(float(pos.market_value or 0)),
        "avg_entry": float(pos.avg_entry_price),
    }


def compute_target_spy(broker: BrokerClient) -> dict:
    """Calculate how much SPY we should hold.

    Target = equity - (PEAD positions value) - (cash reserve)
    """
    equity = broker.portfolio_value()
    cash = broker.cash()
    reserve = equity * config.SPY_CASH_RESERVE_PCT

    # Get all non-SPY positions (PEAD trades)
    positions = broker.get_positions()
    pead_value = sum(
        abs(float(p.market_value or 0))
        for p in positions
        if p.symbol != "SPY"
    )

    spy_pos = get_spy_position(broker)
    spy_current = spy_pos["value"]

    # Target SPY = equity - pead_value - reserve
    spy_target = max(0.0, equity - pead_value - reserve)

    return {
        "equity": equity,
        "cash": cash,
        "reserve": reserve,
        "pead_value": pead_value,
        "spy_current": spy_current,
        "spy_target": spy_target,
        "spy_qty": spy_pos["qty"],
        "diff": spy_target - spy_current,
        "diff_pct": (spy_target - spy_current) / equity if equity > 0 else 0,
    }


def rebalance_to_spy(broker: BrokerClient) -> dict:
    """Buy/sell SPY to match target allocation. Returns action taken."""
    if not config.SPY_BASE_ENABLED:
        return {"action": "disabled"}

    info = compute_target_spy(broker)
    diff_pct = abs(info["diff_pct"])

    # Only rebalance if off by more than the band
    if diff_pct < config.SPY_REBALANCE_BAND:
        log.info(f"SPY base: on-target (current=${info['spy_current']:,.0f} "
                 f"target=${info['spy_target']:,.0f} diff={info['diff_pct']:+.1%})")
        return {"action": "none", "reason": "within band", **info}

    spy_price = broker.get_price("SPY")

    if info["diff"] > 0:
        # Need to BUY more SPY
        buy_dollars = info["diff"]
        qty = max(1, int(buy_dollars / spy_price))
        log.info(f"SPY base: BUYING {qty} shares (${buy_dollars:,.0f} underweight)")
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            order = broker.trade.submit_order(MarketOrderRequest(
                symbol="SPY", qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            log.info(f"SPY base: bought {qty} @ ~${spy_price:.2f}")
            send_trade_alert(
                action="BUY",
                ticker="SPY",
                shares=qty,
                price=spy_price,
                stop=0,
                target=0,
                reason="SPY base rebalance — idle cash deployed",
            )
            return {"action": "buy", "qty": qty, "price": spy_price, **info}
        except Exception as e:
            log.error(f"SPY base buy failed: {e}")
            return {"action": "error", "error": str(e), **info}

    elif info["diff"] < 0:
        # Need to SELL SPY to free cash
        sell_dollars = abs(info["diff"])
        qty = min(info["spy_qty"], max(1, int(sell_dollars / spy_price)))
        log.info(f"SPY base: SELLING {qty} shares (${sell_dollars:,.0f} overweight)")
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            order = broker.trade.submit_order(MarketOrderRequest(
                symbol="SPY", qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            log.info(f"SPY base: sold {qty} @ ~${spy_price:.2f}")
            send_trade_alert(
                action="SELL",
                ticker="SPY",
                shares=qty,
                price=spy_price,
                stop=0,
                target=0,
                reason="SPY base rebalance — reducing SPY (overweight)",
            )
            return {"action": "sell", "qty": qty, "price": spy_price, **info}
        except Exception as e:
            log.error(f"SPY base sell failed: {e}")
            return {"action": "error", "error": str(e), **info}

    return {"action": "none", **info}


def free_cash_for_pead(broker: BrokerClient, amount_needed: float) -> bool:
    """Sell SPY to free up cash for a PEAD entry. Called before PEAD buys."""
    if not config.SPY_BASE_ENABLED:
        return True  # no SPY to sell, cash should be available

    cash = broker.cash()
    if cash >= amount_needed:
        return True  # enough cash already

    shortfall = amount_needed - cash
    spy_pos = get_spy_position(broker)

    if spy_pos["qty"] <= 0:
        log.warning(f"SPY base: need ${shortfall:,.0f} but no SPY to sell")
        return False

    spy_price = broker.get_price("SPY")
    sell_qty = min(spy_pos["qty"], max(1, int(shortfall / spy_price) + 1))

    log.info(f"SPY base: selling {sell_qty} SPY to fund PEAD entry (need ${shortfall:,.0f})")
    try:
        result = broker.sell("SPY", qty=sell_qty)
        sell_price = spy_price
        try:
            sell_price = float(result.get("order", {}).filled_avg_price) or spy_price
        except Exception:
            pass
        send_trade_alert(
            action="SELL",
            ticker="SPY",
            shares=sell_qty,
            price=sell_price,
            stop=0,
            target=0,
            reason=f"SPY base: freed ${shortfall:,.0f} cash for PEAD entry",
        )
        return True
    except Exception as e:
        log.error(f"SPY base: failed to sell SPY for PEAD funding: {e}")
        return False


def log_status(broker: BrokerClient) -> None:
    """Log current SPY base status."""
    if not config.SPY_BASE_ENABLED:
        log.info("SPY base: DISABLED")
        return
    info = compute_target_spy(broker)
    log.info(f"SPY base: ${info['spy_current']:,.0f}/${info['spy_target']:,.0f} "
             f"({info['diff_pct']:+.1%}) | PEAD=${info['pead_value']:,.0f} "
             f"| cash=${info['cash']:,.0f} | reserve=${info['reserve']:,.0f}")
