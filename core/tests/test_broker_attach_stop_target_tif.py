"""Tests for attach_stop_target's time-in-force selection.

Regression: Alpaca rejects a GTC order on a fractional-qty position (error
42210000 "fractional orders must be DAY orders"). attach_stop_target used to
hardcode TimeInForce.GTC, so every fractional buy's protective OCO/stop was
rejected outright, _verify_stop_live never found a live order, and the
caller (market_open.py et al.) flattened the position immediately —
capturing none of the move while bleeding spread + fees. Fractional qty must
use TimeInForce.DAY instead; whole-share qty keeps GTC unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from alpaca.trading.enums import TimeInForce

from core.broker import BrokerClient


def _fake_broker():
    fake = MagicMock(spec=BrokerClient)
    fake.trade = MagicMock()
    fake._verify_stop_live.return_value = True
    return fake


def test_whole_share_oco_uses_gtc():
    fake = _fake_broker()

    BrokerClient.attach_stop_target(fake, "BAC", 10, 57.74, 60.00)

    submitted = fake.trade.submit_order.call_args[0][0]
    assert submitted.time_in_force == TimeInForce.GTC


def test_fractional_share_oco_uses_day():
    fake = _fake_broker()

    BrokerClient.attach_stop_target(fake, "AAPL", 0.0768, 320.0, 350.0)

    submitted = fake.trade.submit_order.call_args[0][0]
    assert submitted.time_in_force == TimeInForce.DAY


def test_fractional_share_simple_stop_uses_day():
    # No target (target=None) path submits a StopOrderRequest instead of OCO.
    fake = _fake_broker()

    BrokerClient.attach_stop_target(fake, "AAPL", 0.0768, 320.0, None)

    submitted = fake.trade.submit_order.call_args[0][0]
    assert submitted.time_in_force == TimeInForce.DAY


def test_whole_share_simple_stop_uses_gtc():
    fake = _fake_broker()

    BrokerClient.attach_stop_target(fake, "BAC", 10, 57.74, None)

    submitted = fake.trade.submit_order.call_args[0][0]
    assert submitted.time_in_force == TimeInForce.GTC
