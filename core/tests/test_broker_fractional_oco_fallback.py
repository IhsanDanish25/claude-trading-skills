"""Regression test for the fractional-qty OCO rejection bug.

Alpaca rejects order_class=OCO outright on any fractional-qty order (error
42210000, "fractional orders must be simple orders" - a distinct restriction
from the already-covered "must be DAY orders" one in
test_broker_attach_stop_target_tif.py). attach_stop_target used to always
attempt OCO whenever both a stop and a target were requested, so every
fractional buy with a take-profit target failed the attach outright,
_verify_stop_live never found a live order, stop_attached came back False,
and the caller's "flatten if stop not attached" guard immediately sold the
position back out - a same-second buy-then-sell that paid the spread twice
for nothing (observed live: AMZN, 2026-08-12 market_open).

Fix: for fractional qty with both stop and target requested, skip the
doomed OCO attempt and attach a SIMPLE stop-only order instead - the
position stays protected (downside covered) even though the automated
take-profit target is given up for that position.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from alpaca.trading.enums import OrderClass, TimeInForce

from core.broker import BrokerClient


def _fake_broker():
    fake = MagicMock(spec=BrokerClient)
    fake.trade = MagicMock()
    fake._verify_stop_live.return_value = True
    return fake


def test_fractional_with_target_attaches_simple_stop_not_oco():
    fake = _fake_broker()

    stop_attached, target_attached = BrokerClient.attach_stop_target(
        fake, "AMZN", 0.039274, 250.41, 299.41
    )

    submitted = fake.trade.submit_order.call_args[0][0]
    assert submitted.order_class == OrderClass.SIMPLE
    assert submitted.time_in_force == TimeInForce.DAY
    assert stop_attached is True
    assert target_attached is False  # take-profit was deliberately dropped, not a failure


def test_fractional_with_target_still_protects_the_downside():
    """The actual bug's symptom: stop_attached must be True so callers don't
    flatten a position that DOES have a live protective stop, just not a
    resting take-profit order."""
    fake = _fake_broker()
    stop_attached, _ = BrokerClient.attach_stop_target(fake, "AMZN", 0.039274, 250.41, 299.41)
    assert stop_attached is True


def test_whole_share_with_target_still_uses_oco():
    """Non-fractional qty is unaffected - still gets the atomic OCO pair."""
    fake = _fake_broker()

    BrokerClient.attach_stop_target(fake, "BAC", 10, 57.74, 60.00)

    submitted = fake.trade.submit_order.call_args[0][0]
    assert submitted.order_class == OrderClass.OCO
    assert submitted.take_profit is not None


def test_fractional_stop_only_unaffected_by_fix():
    """Callers that already pass target=None (e.g. crypto buys, stop-only
    repair passes) behave exactly as before."""
    fake = _fake_broker()

    stop_attached, target_attached = BrokerClient.attach_stop_target(
        fake, "AAPL", 0.0768, 320.0, None
    )

    submitted = fake.trade.submit_order.call_args[0][0]
    assert submitted.order_class == OrderClass.SIMPLE
    assert stop_attached is True
    assert target_attached is False
