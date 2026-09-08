"""Tests for core.protection.reattach_missing_protection.

Closes the gap left after re-enabling fractional buys: a fractional
position's DAY-tif protective exit expires at the session close it was
attached in and can't survive overnight, so it's naked again the next
morning. midday_review's own repair pass doesn't run until noon; this
function is called from market_open (right after open) too, so the gap is
bounded to the overnight hours themselves rather than stretching to noon.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from alpaca.trading.enums import OrderSide, OrderType

import core.protection as protection_module
from core import config
from core.protection import reattach_missing_protection


def _position(symbol, qty, avg_entry_price):
    return SimpleNamespace(symbol=symbol, qty=qty, avg_entry_price=avg_entry_price)


def _order(symbol, otype, side=OrderSide.SELL, order_class=None):
    return SimpleNamespace(symbol=symbol, type=otype, side=side, order_class=order_class)


def _broker(positions, open_orders, price=None, attach_result=(True, True)):
    broker = MagicMock()
    broker.get_positions.return_value = positions
    broker.get_open_orders.return_value = open_orders
    broker.get_price.side_effect = lambda sym: price if price is not None else 0
    broker.attach_stop_target.return_value = attach_result
    return broker


def test_no_positions_is_noop():
    broker = _broker([], [])
    flattened = reattach_missing_protection(broker, config, MagicMock())
    assert flattened == set()
    broker.attach_stop_target.assert_not_called()


def test_attaches_protection_for_fractional_position_missing_a_stop():
    positions = [_position("AAPL", 0.0768, 320.0)]
    broker = _broker(positions, open_orders=[], price=325.0)

    reattach_missing_protection(broker, config, MagicMock())

    broker.attach_stop_target.assert_called_once()
    args = broker.attach_stop_target.call_args[0]
    assert args[0] == "AAPL"
    assert args[1] == 0.0768  # fractional qty preserved, not truncated to 0


def test_skips_position_that_already_has_a_live_stop():
    positions = [_position("AAPL", 0.0768, 320.0)]
    open_orders = [_order("AAPL", OrderType.STOP_LIMIT)]
    broker = _broker(positions, open_orders, price=325.0)

    reattach_missing_protection(broker, config, MagicMock())

    broker.attach_stop_target.assert_not_called()


def test_skips_position_protected_by_an_oco_order():
    """Regression: Alpaca returns an OCO's take-profit leg as the top-level
    order (type=limit, status=accepted) and nests the stop-loss leg as a
    child that only appears with nested=True. Before this exclusion existed,
    a live OCO's take-profit leg had no "stop" in its type, so the symbol
    was never added to stop_orders — reattach_missing_protection treated it
    as unprotected on every tick, cancelled the working OCO, and resubmitted
    an identical one forever."""
    positions = [_position("AAPL", 0.0768, 320.0)]
    open_orders = [_order("AAPL", OrderType.LIMIT, order_class="oco")]
    broker = _broker(positions, open_orders, price=325.0)

    reattach_missing_protection(broker, config, MagicMock())

    broker.attach_stop_target.assert_not_called()


def test_skips_spy_base_symbol():
    positions = [_position("SPY", 5.0, 500.0)]
    broker = _broker(positions, open_orders=[], price=500.0)

    reattach_missing_protection(broker, config, MagicMock())

    broker.attach_stop_target.assert_not_called()


def test_flattens_and_returns_symbol_when_attach_fails():
    positions = [_position("AAPL", 0.0768, 320.0)]
    broker = _broker(positions, open_orders=[], price=325.0, attach_result=(False, False))

    flattened = reattach_missing_protection(broker, config, MagicMock())

    assert flattened == {"AAPL"}
    broker.sell.assert_called_once_with("AAPL", qty=0.0768)


def test_zero_qty_position_is_skipped():
    positions = [_position("AAPL", 0.0, 320.0)]
    broker = _broker(positions, open_orders=[])

    reattach_missing_protection(broker, config, MagicMock())

    broker.attach_stop_target.assert_not_called()


def test_skips_buffett_value_position(monkeypatch):
    """Regression: Buffett Value carries no stop-loss by design (see
    skills/buffett-value/scripts/sell.py). Before this exclusion existed,
    this function would silently attach a normal stop/target bracket to a
    Buffett Value position the very next morning after entry, converting it
    into an ordinary stopped position and bypassing evaluate_exit()'s
    fundamentals/profit-target logic entirely."""
    monkeypatch.setattr(
        protection_module, "buffett_get",
        lambda sym: {"strategy": "buffett_value"} if sym == "CMCSA" else None,
    )
    positions = [_position("CMCSA", 10.0, 26.18)]
    broker = _broker(positions, open_orders=[], price=27.0)

    reattach_missing_protection(broker, config, MagicMock())

    broker.attach_stop_target.assert_not_called()
