"""Tests for BrokerClient's post-submission Alpaca stop-loss verification.

Regression: attach_stop_target used to report stop_attached=True whenever
submit_order() didn't raise, which only proves Alpaca accepted the HTTP
request synchronously — not that the order survived Alpaca's async risk
checks. Live accounts saw stop_attached=True in the logs for BAC/WFC/NKE
with no matching order actually resting at Alpaca."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alpaca.trading.enums import OrderSide, OrderType

from core.broker import BrokerClient


def _order(symbol, otype, side=OrderSide.SELL):
    return SimpleNamespace(symbol=symbol, type=otype, side=side)


def _fake_broker():
    fake = MagicMock(spec=BrokerClient)
    fake.trade = MagicMock()
    return fake


class TestVerifyStopLive:
    def test_finds_live_stop_limit_order(self):
        """attach_stop_target always sets a limit_price, so Alpaca returns
        these as STOP_LIMIT, not STOP — the check must not require an exact
        "stop" type match."""
        fake = _fake_broker()
        fake.get_open_orders.return_value = [_order("BAC", OrderType.STOP_LIMIT)]
        assert BrokerClient._verify_stop_live(fake, "BAC", max_attempts=1) is True

    def test_finds_live_plain_stop_order(self):
        fake = _fake_broker()
        fake.get_open_orders.return_value = [_order("BAC", OrderType.STOP)]
        assert BrokerClient._verify_stop_live(fake, "BAC", max_attempts=1) is True

    def test_returns_false_when_no_matching_order(self):
        fake = _fake_broker()
        fake.get_open_orders.return_value = [_order("WFC", OrderType.LIMIT)]
        assert BrokerClient._verify_stop_live(fake, "BAC", max_attempts=1, delay=0) is False

    def test_returns_false_when_order_missing_after_all_retries(self):
        fake = _fake_broker()
        fake.get_open_orders.return_value = []
        assert BrokerClient._verify_stop_live(fake, "NKE", max_attempts=3, delay=0) is False
        assert fake.get_open_orders.call_count == 3

    def test_ignores_buy_side_orders(self):
        fake = _fake_broker()
        fake.get_open_orders.return_value = [_order("BAC", OrderType.STOP_LIMIT, side=OrderSide.BUY)]
        assert BrokerClient._verify_stop_live(fake, "BAC", max_attempts=1) is False

    def test_survives_get_open_orders_exception(self):
        fake = _fake_broker()
        fake.get_open_orders.side_effect = Exception("network blip")
        assert BrokerClient._verify_stop_live(fake, "BAC", max_attempts=1, delay=0) is False


class TestAttachStopTargetVerification:
    def test_reports_not_attached_when_alpaca_verification_fails(self):
        """Core regression guard: a clean submit_order() call must NOT be
        trusted on its own — attach_stop_target must double-check with
        Alpaca and report failure if the stop isn't actually resting there."""
        fake = _fake_broker()
        fake._verify_stop_live.return_value = False

        stop_attached, target_attached = BrokerClient.attach_stop_target(
            fake, "BAC", 10, 57.74, 60.00
        )

        assert stop_attached is False
        assert target_attached is False
        fake.trade.submit_order.assert_called_once()
        fake._verify_stop_live.assert_called_once_with("BAC")

    def test_reports_attached_when_alpaca_verification_succeeds(self):
        fake = _fake_broker()
        fake._verify_stop_live.return_value = True

        stop_attached, target_attached = BrokerClient.attach_stop_target(
            fake, "BAC", 10, 57.74, 60.00
        )

        assert stop_attached is True
        assert target_attached is True

    def test_does_not_verify_when_submission_itself_raises(self):
        fake = _fake_broker()
        fake.trade.submit_order.side_effect = Exception("account restricted")

        stop_attached, target_attached = BrokerClient.attach_stop_target(
            fake, "BAC", 10, 57.74, 60.00
        )

        assert stop_attached is False
        assert target_attached is False
        fake._verify_stop_live.assert_not_called()
