"""Tests for BrokerClient's post-submission Alpaca stop-loss verification.

Regression: attach_stop_target used to report stop_attached=True whenever
submit_order() didn't raise, which only proves Alpaca accepted the HTTP
request synchronously — not that the order survived Alpaca's async risk
checks. Live accounts saw stop_attached=True in the logs for BAC/WFC/NKE
with no matching order actually resting at Alpaca."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


class TestVerifyStopLiveRetryBudget:
    """Regression: the 2026-08-05 12:07 ET incident flattened BAC/NKE/WFC
    after Alpaca's order-list endpoint hadn't yet caught up with three
    back-to-back OCO submissions — the orders were confirmed live on Alpaca
    (order history shows both legs, cancelled minutes later) but the ~2.5s
    retry budget (6 attempts x 0.5s) ran out before they became visible to
    get_open_orders(). Widen the budget so a slow-but-genuine propagation
    isn't mistaken for a failed attach."""

    def test_default_budget_is_at_least_six_seconds(self):
        sig = inspect.signature(BrokerClient._verify_stop_live)
        max_attempts = sig.parameters["max_attempts"].default
        delay = sig.parameters["delay"].default
        # Sleeps happen between attempts, not after the last one.
        total_wait = (max_attempts - 1) * delay
        assert total_wait >= 6.0, (
            f"retry budget shrank to {total_wait}s (max_attempts={max_attempts}, "
            f"delay={delay}) — the 2026-08-05 flatten incident needs >=6s headroom"
        )

    def test_widened_budget_finds_order_that_appears_late(self):
        """A stop that only becomes visible on, say, the 8th poll (still well
        within the old code's 6-attempt cap it would have missed) must now
        be found rather than triggering a false-negative flatten."""
        fake = _fake_broker()
        calls = {"n": 0}

        def _get_open_orders():
            calls["n"] += 1
            if calls["n"] < 8:
                return []
            return [_order("BAC", OrderType.STOP_LIMIT)]

        fake.get_open_orders.side_effect = _get_open_orders
        with patch("core.broker.time.sleep"):
            assert BrokerClient._verify_stop_live(fake, "BAC") is True
        assert calls["n"] == 8

    def test_still_gives_up_and_returns_false_eventually(self):
        fake = _fake_broker()
        fake.get_open_orders.return_value = []
        with patch("core.broker.time.sleep") as mock_sleep:
            assert BrokerClient._verify_stop_live(fake, "BAC") is False
        # Confirms retries actually still happen (budget isn't infinite) and
        # each sleep uses the (now-larger) default delay.
        sig = inspect.signature(BrokerClient._verify_stop_live)
        assert mock_sleep.call_count == sig.parameters["max_attempts"].default - 1


class TestVerifyStopLiveByOrderId:
    """Regression: TSL and NVDA both flattened on 2026-08-11 despite the
    submit succeeding and Alpaca's order history later confirming the OCO
    was live — the list-scan in get_open_orders() just hadn't caught up yet
    when _verify_stop_live checked it, and cancel-then-resubmit (the
    40310000 retry path) makes that propagation lag worse. Checking the
    exact submitted order_id via get_order_by_id sidesteps the aggregate
    list's lag entirely."""

    def test_finds_order_live_by_id_even_when_list_scan_would_miss_it(self):
        fake = _fake_broker()
        fake.get_open_orders.return_value = []  # list hasn't caught up
        fake.trade.get_order_by_id.return_value = SimpleNamespace(status="held")
        assert BrokerClient._verify_stop_live(fake, "TSL", order_id="abc123", max_attempts=1) is True

    def test_terminal_status_is_not_live(self):
        fake = _fake_broker()
        fake.trade.get_order_by_id.return_value = SimpleNamespace(status="canceled")
        assert (
            BrokerClient._verify_stop_live(fake, "TSL", order_id="abc123", max_attempts=1, delay=0)
            is False
        )

    def test_get_order_by_id_exception_falls_through_to_retry(self):
        fake = _fake_broker()
        fake.trade.get_order_by_id.side_effect = Exception("network blip")
        assert (
            BrokerClient._verify_stop_live(fake, "TSL", order_id="abc123", max_attempts=1, delay=0)
            is False
        )

    def test_falls_back_to_list_scan_when_no_order_id(self):
        fake = _fake_broker()
        fake.get_open_orders.return_value = [_order("BAC", OrderType.STOP_LIMIT)]
        assert BrokerClient._verify_stop_live(fake, "BAC", max_attempts=1) is True
        fake.trade.get_order_by_id.assert_not_called()


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
        fake._verify_stop_live.assert_called_once_with(
            "BAC", order_id=fake.trade.submit_order.return_value.id
        )

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
