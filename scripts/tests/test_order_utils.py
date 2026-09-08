"""Tests for core.order_utils.order_field()."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alpaca.trading.enums import OrderSide, OrderType

from core.order_utils import order_field


class TestOrderField:
    def test_normalizes_alpaca_enums(self):
        o = SimpleNamespace(side=OrderSide.SELL, type=OrderType.STOP)
        assert order_field(o, "side") == "sell"
        assert order_field(o, "type") == "stop"

    def test_normalizes_trailing_stop(self):
        o = SimpleNamespace(type=OrderType.TRAILING_STOP)
        assert order_field(o, "type") == "trailing_stop"

    def test_passes_through_raw_strings(self):
        o = SimpleNamespace(side="SELL")
        assert order_field(o, "side") == "sell"

    def test_missing_or_none_field_is_empty(self):
        assert order_field(SimpleNamespace(), "side") == ""
        assert order_field(SimpleNamespace(side=None), "side") == ""

    def test_str_of_enum_is_not_the_value(self):
        """Documents why this helper exists: alpaca-py enums are str mixins
        whose str() includes the class name."""
        assert str(OrderSide.SELL).lower() != "sell"
