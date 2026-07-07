"""Tests for market_close order matching and SPY base exclusion."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpaca.trading.enums import OrderSide, OrderType

import market_close


def _order(symbol, otype, side=OrderSide.SELL, stop_price=None):
    return SimpleNamespace(symbol=symbol, type=otype, side=side,
                           stop_price=stop_price)


class TestBuildStopMap:
    def test_maps_real_enum_stop_orders(self):
        """Regression: str(enum) comparisons meant stop_map was always empty,
        so the 14:45 late-trail ratchet never saw existing stops."""
        orders = [
            _order("AAPL", OrderType.STOP, stop_price=95.0),
            _order("MSFT", OrderType.LIMIT, stop_price=None),
            _order("NVDA", OrderType.STOP, side=OrderSide.BUY, stop_price=88.0),
        ]
        assert market_close._build_stop_map(orders) == {"AAPL": 95.0}

    def test_ignores_orders_without_numeric_stop(self):
        orders = [_order("AAPL", OrderType.STOP, stop_price=None)]
        assert market_close._build_stop_map(orders) == {}
