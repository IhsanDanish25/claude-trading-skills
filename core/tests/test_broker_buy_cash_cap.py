"""Unit tests for BrokerClient.buy() cash/buying-power guardrail.

buy() previously sized orders off equity (portfolio_value, which includes
the market value of existing positions) and clamped only against
MAX_POSITION_SIZE_PCT of that equity. On an account that is already mostly
invested, equity can far exceed actual spendable cash, so a qty sized off
equity alone could exceed buying power and get rejected — or draw on
margin — at submission time. These tests exercise the added guardrail that
clamps (or blocks) the order against BrokerClient.buying_power().

Pure-logic tests: BrokerClient is built via object.__new__ to skip
TradingClient/StockHistoricalDataClient construction (no network, no API
keys), and a fake trade client stands in for order submission/polling.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.broker import BrokerClient


class _FakeTradeClient:
    def __init__(self, fill_price: float):
        self._fill_price = fill_price
        self.submitted = []

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="order-1")

    def get_order_by_id(self, order_id):
        qty = self.submitted[-1].qty
        return SimpleNamespace(filled_avg_price=self._fill_price, filled_qty=qty)


def _make_broker(
    *,
    ref_price: float,
    equity: float,
    buying_power: float,
    existing_position=None,
    position_count: int = 0,
) -> BrokerClient:
    broker = object.__new__(BrokerClient)
    broker.trade = _FakeTradeClient(fill_price=ref_price)
    broker.get_price = lambda symbol: ref_price
    broker.portfolio_value = lambda: equity
    broker.buying_power = lambda: buying_power
    broker.get_position = lambda symbol: existing_position
    broker.position_count = lambda: position_count
    broker.is_market_open = lambda: True
    broker.attach_stop_target = lambda symbol, qty, stop, target: (True, True)
    return broker


def test_buy_unaffected_when_cash_covers_sized_order():
    # equity=$100k, 5% cap -> $5k notional cap; plenty of cash available.
    broker = _make_broker(ref_price=50.0, equity=100_000.0, buying_power=80_000.0)

    result = broker.buy("AAPL", shares=20, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result.get("blocked") is None
    assert result["qty"] == 20
    assert broker.trade.submitted[-1].qty == 20


def test_buy_clamped_when_order_exceeds_available_cash():
    # Requested 20 shares @ $50 = $1000, but only $300 of buying power exists.
    broker = _make_broker(ref_price=50.0, equity=100_000.0, buying_power=300.0)

    result = broker.buy("AAPL", shares=20, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result.get("blocked") is None
    assert result["qty"] == 6  # int(300 / 50)
    assert broker.trade.submitted[-1].qty == 6


def test_buy_blocked_when_cash_cannot_afford_one_share():
    broker = _make_broker(ref_price=50.0, equity=100_000.0, buying_power=10.0)

    result = broker.buy("AAPL", shares=20, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result == {"blocked": True, "reason": "insufficient_cash"}
    assert broker.trade.submitted == []


def test_buy_cash_cap_applies_after_risk_parity_sizing():
    # No explicit shares: risk-parity/size-pct sizing picks a qty, which the
    # cash guardrail must still clamp down.
    broker = _make_broker(ref_price=100.0, equity=100_000.0, buying_power=250.0)

    result = broker.buy("AAPL", stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result.get("blocked") is None
    assert result["qty"] == 2  # int(250 / 100)
    assert broker.trade.submitted[-1].qty == 2
