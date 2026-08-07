"""Shared "does every open position have a live stop?" repair pass.

Originally inline in midday_review.py only. Fractional (DAY-tif) protective
exits expire at the close of the session they were attached in — unlike a
whole-share GTC stop, they cannot survive overnight — so a fractional
position that carried a live stop at yesterday's close is naked again by
the time market_open runs today. midday_review's own repair pass doesn't
run until noon, leaving a ~2.5hr gap after the 9:30 open with no check at
all. Both routines call this so the gap is closed at both checkpoints.
"""

from __future__ import annotations

from core.notifier import send_trade_alert
from core.order_utils import order_field
from core.safe_oco_attach import cancel_open_sell_orders
from core.spy_base import is_base_symbol


def reattach_missing_protection(broker, config, log) -> set[str]:
    """For every open position without a live resting stop order, compute a
    fresh stop/target off the tighter of entry vs. current price and attach
    it. A position whose attach fails (and fails Alpaca verification) is
    flattened rather than left naked. Returns the set of flattened symbols.

    Skips SPY base holdings (managed separately by spy_base) and any
    position that already has a live stop — including a same-day fractional
    DAY stop, so this never overwrites a fresher trailing stop, and a
    whole-share GTC stop from a prior day is left alone.
    """
    flattened: set[str] = set()
    positions = broker.get_positions()
    if not positions:
        return flattened

    open_orders = broker.get_open_orders() or []
    stop_orders = set()
    for o in open_orders:
        try:
            if "stop" in order_field(o, "type") and order_field(o, "side") == "sell":
                stop_orders.add(o.symbol)
        except Exception:
            pass

    for p in positions:
        sym = p.symbol
        if is_base_symbol(sym):
            continue
        if sym in stop_orders:
            continue

        qty = float(p.qty)
        if qty <= 0:
            continue

        entry = float(p.avg_entry_price)
        anchor = entry
        try:
            cur = broker.get_price(sym)
            if cur > 0:
                anchor = min(entry, cur)
        except Exception as e:
            log.warning(f"  {sym}: price check failed ({e}) — anchoring on entry")

        stop = round(anchor * (1 - config.STOP_LOSS_PCT), 2)
        target = round(entry * (1 + config.TAKE_PROFIT_PCT), 2)
        log.info(
            f"  Attaching protection: {sym} (entry=${entry:.2f} stop=${stop} target=${target})"
        )

        stop_attached, _ = broker.attach_stop_target(sym, qty, stop, target)
        if stop_attached:
            continue

        log.error(
            f"  ✗ {sym}: stop-loss attach FAILED (verified against Alpaca) "
            f"— flattening to avoid an unprotected position"
        )
        try:
            cancel_open_sell_orders(broker, sym)
            broker.sell(sym, qty=qty)
            send_trade_alert(
                action="FLATTEN",
                ticker=sym,
                shares=qty,
                price=anchor,
                stop=stop,
                target=target,
                reason="Stop-loss re-attach failed — position closed to avoid naked exposure",
            )
            flattened.add(sym)
        except Exception as e:
            log.error(
                f"  ✗ {sym}: flatten-on-attach-failure ALSO failed: {e} "
                f"— position remains unprotected, needs manual review"
            )

    return flattened
