"""Tests for OCO protection-attach logic in core/broker.py.

Covers the "insufficient qty available" (Alpaca error 40310000) bug: a
symbol's shares can already be reserved by an existing open order (e.g. a
plain SELL LIMIT left over from a partial fill or a stale bracket leg),
which causes a fresh OCO submission to be rejected. The fix cancels any
open orders for the symbol first, retries once on 40310000, skips churn
when a matching OCO is already in place, and alerts when protection still
can't be attached.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alpaca.common.exceptions import APIError

from core.broker import BrokerClient


def make_broker() -> BrokerClient:
    """Construct a BrokerClient without hitting the network/__init__."""
    broker = BrokerClient.__new__(BrokerClient)
    broker.trade = MagicMock()
    broker.data = MagicMock()
    return broker


def make_order(symbol="PANW", side="sell", order_type="limit", order_class="simple",
                qty=10, stop_price=None, limit_price=None, order_id="order-1"):
    return SimpleNamespace(
        id=order_id, symbol=symbol, side=side, type=order_type,
        order_class=order_class, qty=qty, stop_price=stop_price,
        limit_price=limit_price,
    )


def insufficient_qty_error() -> APIError:
    return APIError('{"code": 40310000, "message": "insufficient qty available"}')


# ── attach_stop_target: cancel-first ────────────────────────────────────────

class TestAttachStopTargetCancelsFirst:
    def test_cancels_existing_plain_order_before_submitting_oco(self):
        broker = make_broker()
        stale_order = make_order(order_type="limit", order_class="simple", order_id="stale-1")
        # First poll (inside _cancel_symbol_orders, pre-cancel listing) sees the
        # stale order; subsequent polls (after cancel) see it gone.
        broker.trade.get_orders.side_effect = [
            [stale_order],  # _find_oco_legs / pre-cancel snapshot
            [stale_order],  # snapshot right before cancelling
            [],             # first poll after cancel — cleared
        ]

        ok_stop, ok_target = broker.attach_stop_target("PANW", 10, 340.0, 360.0)

        assert (ok_stop, ok_target) == (True, True)
        broker.trade.cancel_order_by_id.assert_called_once_with("stale-1")
        broker.trade.submit_order.assert_called_once()

    def test_idempotent_skip_when_matching_oco_already_open(self):
        broker = make_broker()
        stop_leg = make_order(order_type="stop", order_class="oco", qty=10,
                               stop_price=340.0, order_id="stop-1")
        target_leg = make_order(order_type="limit", order_class="oco", qty=10,
                                 limit_price=360.0, order_id="target-1")
        broker.trade.get_orders.return_value = [stop_leg, target_leg]

        ok_stop, ok_target = broker.attach_stop_target("PANW", 10, 340.0, 360.0)

        assert (ok_stop, ok_target) == (True, True)
        broker.trade.cancel_order_by_id.assert_not_called()
        broker.trade.submit_order.assert_not_called()

    def test_does_not_skip_when_existing_oco_prices_are_stale(self):
        broker = make_broker()
        stop_leg = make_order(order_type="stop", order_class="oco", qty=10,
                               stop_price=300.0, order_id="stop-1")
        target_leg = make_order(order_type="limit", order_class="oco", qty=10,
                                 limit_price=320.0, order_id="target-1")
        broker.trade.get_orders.side_effect = [
            [stop_leg, target_leg],
            [stop_leg, target_leg],
            [],
        ]

        ok_stop, ok_target = broker.attach_stop_target("PANW", 10, 340.0, 360.0)

        assert (ok_stop, ok_target) == (True, True)
        assert broker.trade.cancel_order_by_id.call_count == 2
        broker.trade.submit_order.assert_called_once()


# ── attach_stop_target: retry + alert ───────────────────────────────────────

class TestAttachStopTargetRetry:
    def test_retries_once_on_40310000_then_succeeds(self, monkeypatch):
        broker = make_broker()
        broker.trade.get_orders.return_value = []
        broker.trade.submit_order.side_effect = [insufficient_qty_error(), None]
        sleeps = []
        monkeypatch.setattr("core.broker.time.sleep", lambda s: sleeps.append(s))

        ok_stop, ok_target = broker.attach_stop_target("PANW", 10, 340.0, 360.0)

        assert (ok_stop, ok_target) == (True, True)
        assert broker.trade.submit_order.call_count == 2
        assert sleeps == [2]

    def test_alerts_after_final_failure(self, monkeypatch):
        broker = make_broker()
        broker.trade.get_orders.return_value = []
        broker.trade.submit_order.side_effect = insufficient_qty_error()
        monkeypatch.setattr("core.broker.time.sleep", lambda s: None)
        alert = MagicMock(return_value=True)
        monkeypatch.setattr("core.broker.send_error_alert", alert)

        ok_stop, ok_target = broker.attach_stop_target("PANW", 10, 340.0, 360.0)

        assert (ok_stop, ok_target) == (False, False)
        assert broker.trade.submit_order.call_count == 2
        alert.assert_called_once()
        assert "PANW" in alert.call_args.kwargs.get("error", alert.call_args.args[-1] if alert.call_args.args else "")

    def test_non_qty_error_does_not_retry_or_alert(self, monkeypatch):
        broker = make_broker()
        broker.trade.get_orders.return_value = []
        broker.trade.submit_order.side_effect = RuntimeError("network blip")
        alert = MagicMock(return_value=True)
        monkeypatch.setattr("core.broker.send_error_alert", alert)

        ok_stop, ok_target = broker.attach_stop_target("PANW", 10, 340.0, 360.0)

        assert (ok_stop, ok_target) == (False, False)
        assert broker.trade.submit_order.call_count == 1
        alert.assert_not_called()


# ── tighten_stop: rebuild full OCO when no stop leg exists ──────────────────

class TestTightenStopRebuildsOco:
    def test_rebuilds_oco_when_position_held_by_plain_sell_order(self):
        broker = make_broker()
        plain_order = make_order(order_type="limit", order_class="simple", order_id="plain-1")
        broker.trade.get_orders.return_value = [plain_order]
        broker.get_position = MagicMock(
            return_value=SimpleNamespace(qty="10", avg_entry_price="350.0")
        )
        broker.attach_stop_target = MagicMock(return_value=(True, True))

        ok = broker.tighten_stop("PANW", 345.0, target=360.0)

        assert ok is True
        broker.attach_stop_target.assert_called_once_with("PANW", 10, 345.0, 360.0)

    def test_rebuild_returns_false_when_no_position(self):
        broker = make_broker()
        broker.trade.get_orders.return_value = []
        broker.get_position = MagicMock(return_value=None)
        broker.attach_stop_target = MagicMock()

        ok = broker.tighten_stop("PANW", 345.0, target=360.0)

        assert ok is False
        broker.attach_stop_target.assert_not_called()

    def test_replaces_directly_when_stop_order_already_exists(self):
        broker = make_broker()
        stop_order = make_order(order_type="stop", order_class="oco",
                                 stop_price=340.0, order_id="stop-1")
        broker.trade.get_orders.return_value = [stop_order]
        broker.attach_stop_target = MagicMock()

        ok = broker.tighten_stop("PANW", 345.0, target=360.0)

        assert ok is True
        broker.trade.replace_order_by_id.assert_called_once()
        broker.attach_stop_target.assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
