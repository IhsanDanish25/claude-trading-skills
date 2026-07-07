"""Cancel-first + retry wrapper for OCO stop/target submission.

Alpaca rejects a new OCO sell order with error 40310000 ("insufficient qty
available for order") whenever a stale or orphaned sell order for the same
symbol is still holding the shares the new order needs — e.g. a leftover
order from a previous failed attempt, or a redeploy that re-ran a routine.
midday_review.py already worked around this ad hoc by cancelling open sell
orders before its repair loop; this module makes that recovery reusable so
BrokerClient.attach_stop_target can retry safely too.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from core.order_utils import order_field

log = logging.getLogger(__name__)

_INSUFFICIENT_QTY_MARKERS = ("40310000", "insufficient qty")


def _is_insufficient_qty_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _INSUFFICIENT_QTY_MARKERS)


def cancel_open_sell_orders(broker, symbol: str) -> int:
    """Cancel open SELL orders for symbol that could be locking shares needed
    by an OCO attach or a clean position close. Returns the number actually
    cancelled. Public so callers outside this module (e.g. an ad hoc close
    script) can reuse the same cancel-first safety before closing a position,
    instead of duplicating the Alpaca open-orders filtering logic."""
    try:
        open_orders = broker.get_open_orders()
    except Exception as e:
        log.warning("safe_oco_attach: could not list open orders for %s: %s", symbol, e)
        return 0

    cancelled = 0
    for o in open_orders:
        if o.symbol != symbol:
            continue
        if order_field(o, "side") != "sell":
            continue
        try:
            broker.trade.cancel_order_by_id(o.id)
            cancelled += 1
            log.info("safe_oco_attach: cancelled stale sell order %s for %s", o.id, symbol)
        except Exception as e:
            log.warning("safe_oco_attach: cancel %s for %s failed: %s", o.id, symbol, e)
    return cancelled


def safe_attach_oco(broker, symbol: str, qty: int, stop: float, target: float,
                     submit_fn: Callable[[], object],
                     max_retries: int = 2, retry_delay: float = 1.0) -> bool:
    """Call submit_fn() (the raw OCO order submission), retrying on Alpaca's
    40310000 "insufficient qty" failure by cancelling stale open sell orders
    for symbol first. Any other exception is NOT retried and re-raises
    immediately. Re-raises once max_retries is exhausted.

    stop/target are accepted (not used directly here) so log messages have
    the full context of what was being attached when a retry happens.
    """
    attempt = 0
    while True:
        try:
            submit_fn()
            return True
        except Exception as e:
            if not _is_insufficient_qty_error(e) or attempt >= max_retries:
                raise
            attempt += 1
            log.warning(
                "safe_oco_attach: %s attach failed (attempt %d/%d, stop=%.2f target=%.2f) — "
                "%s — cancelling stale sell orders and retrying",
                symbol, attempt, max_retries, stop, target, e,
            )
            cancel_open_sell_orders(broker, symbol)
            if retry_delay:
                time.sleep(retry_delay)
