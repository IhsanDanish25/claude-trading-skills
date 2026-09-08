"""Unit tests for core.safe_oco_attach — cancel-first + retry OCO submission.

Alpaca error 40310000 ("insufficient qty available for order") fires when a
stale/orphan sell order is still holding the shares a new OCO attach needs.
This module retries the submission after cancelling those blocking orders.
Pure-logic tests: no live Alpaca access, a fake broker stands in for
BrokerClient.
"""

from __future__ import annotations

from types import SimpleNamespace

from alpaca.trading.enums import OrderSide

import pytest

from core.safe_oco_attach import safe_attach_oco


class _FakeTradeClient:
    def __init__(self):
        self.cancelled_ids = []

    def cancel_order_by_id(self, order_id):
        self.cancelled_ids.append(order_id)


class _FakeBroker:
    def __init__(self, open_orders=None):
        self.trade = _FakeTradeClient()
        self._open_orders = open_orders or []

    def get_open_orders(self):
        return self._open_orders


def _order(id_, symbol, side=OrderSide.SELL):
    # Real alpaca enums, not plain strings: str(OrderSide.SELL) is
    # 'OrderSide.SELL', and string mocks masked a filter that matched nothing.
    return SimpleNamespace(id=id_, symbol=symbol, side=side)


def test_success_on_first_try_does_not_cancel_anything():
    broker = _FakeBroker(open_orders=[_order("o1", "AAPL")])
    calls = []
    submitted_order = SimpleNamespace(id="new-order-1")

    def submit():
        calls.append(1)
        return submitted_order

    result = safe_attach_oco(broker, "AAPL", 10, 90.0, 110.0, submit)

    # safe_attach_oco returns submit_fn()'s own return value (the submitted
    # order) so callers can verify that exact order by ID, instead of a bare
    # True that discards which order actually ended up live.
    assert result is submitted_order
    assert calls == [1]
    assert broker.trade.cancelled_ids == []


def test_insufficient_qty_error_cancels_stale_sell_orders_then_retries():
    broker = _FakeBroker(open_orders=[
        _order("stale-1", "AAPL", side=OrderSide.SELL),
        _order("other-buy", "AAPL", side=OrderSide.BUY),   # wrong side — not cancelled
        _order("other-sym", "MSFT", side=OrderSide.SELL),  # wrong symbol — not cancelled
    ])
    attempts = []
    submitted_order = SimpleNamespace(id="new-order-2")

    def submit():
        attempts.append(1)
        if len(attempts) == 1:
            raise Exception('{"code":40310000,"message":"insufficient qty available for order"}')
        return submitted_order

    result = safe_attach_oco(broker, "AAPL", 10, 90.0, 110.0, submit, retry_delay=0)

    assert result is submitted_order
    assert len(attempts) == 2
    assert broker.trade.cancelled_ids == ["stale-1"]


def test_non_retryable_error_propagates_without_cancelling():
    broker = _FakeBroker(open_orders=[_order("o1", "AAPL")])

    def submit():
        raise Exception("insufficient buying power")

    with pytest.raises(Exception, match="insufficient buying power"):
        safe_attach_oco(broker, "AAPL", 10, 90.0, 110.0, submit, retry_delay=0)

    assert broker.trade.cancelled_ids == []


def test_retry_log_does_not_crash_when_target_is_none():
    """Regression: the retry-path log line used %.2f on `target` unconditionally,
    which raises TypeError when target=None (the stop-only attach path -
    e.g. a fractional position, or an explicit stop-only repair call).
    Retrying that call must not itself crash with a formatting error."""
    broker = _FakeBroker(open_orders=[_order("stale-1", "AAPL")])
    attempts = []
    submitted_order = SimpleNamespace(id="new-order-3")

    def submit():
        attempts.append(1)
        if len(attempts) == 1:
            raise Exception('{"code":40310000,"message":"insufficient qty available for order"}')
        return submitted_order

    result = safe_attach_oco(broker, "AAPL", 0.0768, 320.0, None, submit, retry_delay=0)

    assert result is submitted_order
    assert len(attempts) == 2


def test_persistent_insufficient_qty_raises_after_exhausting_retries():
    broker = _FakeBroker(open_orders=[_order("stale-1", "AAPL")])
    attempts = []

    def submit():
        attempts.append(1)
        raise Exception("40310000 insufficient qty available for order")

    with pytest.raises(Exception, match="40310000"):
        safe_attach_oco(broker, "AAPL", 10, 90.0, 110.0, submit, max_retries=2, retry_delay=0)

    assert len(attempts) == 3  # initial attempt + 2 retries
    assert broker.trade.cancelled_ids == ["stale-1", "stale-1"]
