"""Tests for the midday protection-attach loop's SPY base handling."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import midday_review
from core import config
from alpaca.trading.enums import OrderSide


def _order(symbol, side, oid="oid-1"):
    o = MagicMock()
    o.symbol = symbol
    o.side = side
    o.id = oid
    return o


class TestClearBaseProtection:
    """Regression: midday attached a protection OCO to the full SPY base
    holding, which held every share and blocked spy_base rebalance sells
    (Alpaca 40310000 insufficient qty)."""

    def test_cancels_legacy_sell_orders_on_base_symbol(self, monkeypatch):
        """Orders carry real alpaca OrderSide enums: str(OrderSide.SELL) is
        'OrderSide.SELL', NOT 'sell' — a plain-string mock here masked a bug
        where the cancel matched nothing in production (2026-07-07 13:58 log)."""
        monkeypatch.setattr(config, "SPY_BASE_ENABLED", True)
        broker = MagicMock()
        orders = [_order("SPY", OrderSide.SELL, "a"), _order("SPY", OrderSide.BUY, "b"),
                  _order("AAPL", OrderSide.SELL, "c")]

        cancelled = midday_review._clear_base_protection(broker, orders, "SPY")

        assert cancelled == 1
        broker.trade.cancel_order_by_id.assert_called_once_with("a")

    def test_matches_raw_string_sides_too(self, monkeypatch):
        monkeypatch.setattr(config, "SPY_BASE_ENABLED", True)
        broker = MagicMock()
        orders = [_order("SPY", "SELL", "a"), _order("SPY", "sell", "b")]

        assert midday_review._clear_base_protection(broker, orders, "SPY") == 2

    def test_order_side_normalizes_enum_and_string(self):
        assert midday_review._order_field(_order("SPY", OrderSide.SELL), "side") == "sell"
        assert midday_review._order_field(_order("SPY", "SELL"), "side") == "sell"
        assert midday_review._order_field(MagicMock(spec=[]), "side") == ""

    def test_no_orders_is_noop(self, monkeypatch):
        monkeypatch.setattr(config, "SPY_BASE_ENABLED", True)
        broker = MagicMock()
        assert midday_review._clear_base_protection(broker, None, "SPY") == 0
        broker.trade.cancel_order_by_id.assert_not_called()

    def test_cancel_failure_is_nonfatal(self, monkeypatch):
        monkeypatch.setattr(config, "SPY_BASE_ENABLED", True)
        broker = MagicMock()
        broker.trade.cancel_order_by_id.side_effect = RuntimeError("gone")
        orders = [_order("SPY", "sell", "a")]
        assert midday_review._clear_base_protection(broker, orders, "SPY") == 0
