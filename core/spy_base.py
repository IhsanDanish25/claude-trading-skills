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
import uuid

from core import config
from core.broker import BrokerClient
from core.notifier import send_trade_alert

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


def _cancel_stale_spy_orders(broker: BrokerClient) -> int:
    """Cancel open SELL orders that are holding SPY shares, then retry.
    Called when rebalance_to_spy hits a 40310000 insufficient-qty error.
    Returns the number cancelled; any cancellation failure is non-fatal."""
    try:
        open_orders = broker.get_open_orders()
    except Exception:
        return 0
    cancelled = 0
    for o in open_orders:
        if getattr(o, "symbol", None) != "SPY":
            continue
        # Use broker.trade directly to avoid BrokerClient.get_open_orders()'s
        # QueryOrderStatus filter — we want ALL open orders here.
        if _order_side(o) != "sell":
            continue
        try:
            broker.trade.cancel_order_by_id(str(o.id))
            cancelled += 1
        except Exception:
            pass
    return cancelled


def _order_side(o) -> str:
    """Map Alpaca OrderSide enum (or dict) to a clean lowercase string."""
    raw = getattr(o, "side", None)
    if raw is None:
        return ""
    s = str(raw)
    # 'OrderSide.SELL'  or  'sell'  or  '{"value": "sell"}'
    if "." in s:
        return s.rsplit(".", 1)[-1].lower()
    return s.strip().lower()


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

    # SPY governance: refuse to buy if it would push SPY above SPY_MAX_PCT
    spy_pct = (info["spy_current"] + info["diff"]) / info["equity"] if info["equity"] > 0 else 0
    max_pct = getattr(config, 'SPY_MAX_PCT', 0.93)
    if spy_pct > max_pct:
        log.warning("SPY BUY BLOCKED —would make SPY %.0f%% of equity (> %.0f%% cap)", spy_pct*100, max_pct*100)
        return {"action": "exceeded_max_pct", "reason": f"SPY would be {spy_pct:.0%}>max{max_pct:.0%}", **info}

    if info["diff"] > 0:
        # Need to BUY more SPY
        buy_dollars = info["diff"]
        qty = max(1, int(buy_dollars / spy_price))

        # Clamp to available buying power — mirrors the guardrail
        # BrokerClient.buy() has (added for the same failure mode: sizing off
        # the equity/target diff alone can exceed actual spendable cash).
        # Skip (don't force qty=1) rather than submit an order Alpaca will reject.
        available_cash = broker.buying_power()
        order_value = qty * spy_price
        if order_value > available_cash:
            cash_qty = max(0, int(available_cash / spy_price))
            if cash_qty < 1:
                log.warning(
                    "SPY base BUY SKIPPED — insufficient buying power ($%.2f) for 1 share @ $%.2f",
                    available_cash, spy_price,
                )
                return {"action": "insufficient_cash", "reason": "insufficient buying power", **info}
            log.warning(
                "SPY base BUY CLAMPED — %d shares ($%.0f) exceeds buying power ($%.0f); reduced to %d shares",
                qty, order_value, available_cash, cash_qty,
            )
            qty = cash_qty

        log.info(f"SPY base: BUYING {qty} shares (${buy_dollars:,.0f} underweight)")
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
            broker.trade.submit_order(MarketOrderRequest(
                symbol="SPY", qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
                client_order_id=f"spy-rebal-buy-{qty}",
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
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
            broker.trade.submit_order(MarketOrderRequest(
                symbol="SPY", qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                client_order_id=f"spy-rebal-sell-{qty}",
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
            err_str = str(e).lower()
            if "40310000" in err_str or "insufficient qty" in err_str:
                # Shares are locked in a stale OCO. Cancel all open SPY sell
                # orders and retry once. This replicates safe_oco_attach's
                # cancel-then-retry pattern for the SPY rebalance path.
                import time as _time
                cancelled = _cancel_stale_spy_orders(broker)
                log.warning(f"SPY base sell: 40310000 — cancelled {cancelled} stale "
                            f"sell orders, retrying ({e})")
                _time.sleep(1)
                try:
                    broker.trade.submit_order(MarketOrderRequest(
                        symbol="SPY", qty=qty, side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC,
                        client_order_id=f"spy-rebal-sell-retry-{qty}",
                    ))
                    log.info(f"SPY base: sold {qty} @ ~${spy_price:.2f} (retry after cancel)")
                    send_trade_alert(
                        action="SELL",
                        ticker="SPY",
                        shares=qty,
                        price=spy_price,
                        stop=0,
                        target=0,
                        reason="SPY base rebalance — reducing SPY (retry after stale order cancel)",
                    )
                    return {"action": "sell", "qty": qty, "price": spy_price, **info}
                except Exception as retry_err:
                    log.error(f"SPY base sell (retry) failed: {retry_err}")
                    return {"action": "error", "error": str(retry_err), **info}
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
    import time as _time

    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    def _do_sell():
        return broker.trade.submit_order(MarketOrderRequest(
            symbol="SPY", qty=sell_qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            client_order_id=f"spy-pead-sell-{sell_qty}",
        ))

    try:
        order = _do_sell()
        sell_price = spy_price
        try:
            sell_price = float(getattr(order, "filled_avg_price", None) or spy_price)
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
        err_str = str(e).lower()
        if "40310000" in err_str or "insufficient qty" in err_str:
            cancelled = _cancel_stale_spy_orders(broker)
            log.warning(f"SPY base: 40310000 — cancelled {cancelled} stale sell orders, retrying")
            _time.sleep(1)
            try:
                order = _do_sell()
                try:
                    sell_price = float(getattr(order, "filled_avg_price", None) or spy_price)
                except Exception:
                    sell_price = spy_price
                send_trade_alert(
                    action="SELL",
                    ticker="SPY",
                    shares=sell_qty,
                    price=sell_price,
                    stop=0,
                    target=0,
                    reason=f"SPY base: freed ${shortfall:,.0f} cash for PEAD entry (retry)",
                )
                return True
            except Exception as retry_err:
                log.error(f"SPY base: PEAD funding sell (retry) failed: {retry_err}")
                return False
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
