"""Tests for BrokerClient.tighten_stop order matching."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alpaca.trading.enums import OrderSide, OrderType

from core.broker import BrokerClient


def _stop_order(oid, symbol, stop_price=100.0):
    return SimpleNamespace(id=oid, symbol=symbol, side=OrderSide.SELL,
                           type=OrderType.STOP, stop_price=stop_price,
                           limit_price=None, order_class="oco")


def _fake_broker(open_orders):
    fake = MagicMock(spec=BrokerClient)
    fake.get_open_orders.return_value = open_orders
    fake.trade = MagicMock()
    return fake


class TestTightenStop:
    def test_replaces_stop_with_real_enum_orders(self):
        """Regression: side/type were compared via str(enum).lower(), which
        yields 'orderside.sell' — no candidate ever matched, so tighten_stop
        always returned False and trailing stops never tightened."""
        fake = _fake_broker([_stop_order("s1", "AAPL", 95.0)])

        result = BrokerClient.tighten_stop(fake, "AAPL", 97.5)

        assert result is True
        fake.trade.replace_order_by_id.assert_called_once()
        assert fake.trade.replace_order_by_id.call_args[0][0] == "s1"

    def test_returns_false_when_no_stop_exists(self):
        fake = _fake_broker([SimpleNamespace(id="b1", symbol="AAPL",
                                             side=OrderSide.BUY,
                                             type=OrderType.LIMIT,
                                             stop_price=None, limit_price=100.0,
                                             order_class="simple")])
        assert BrokerClient.tighten_stop(fake, "AAPL", 97.5) is False
        fake.trade.replace_order_by_id.assert_not_called()
