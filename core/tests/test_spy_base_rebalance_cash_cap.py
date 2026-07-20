"""Unit tests for rebalance_to_spy()'s BUY-branch buying-power guardrail.

rebalance_to_spy() sized its BUY branch off the equity-vs-target dollar diff
alone (qty = max(1, int(buy_dollars / spy_price))) and submitted straight to
broker.trade.submit_order(), never checking BrokerClient.buying_power() —
unlike BrokerClient.buy(), which got that exact guardrail in a prior fix
(core/tests/test_broker_buy_cash_cap.py). On an already-invested account,
buying_power can be far smaller than the equity-sized diff, so this path
could submit a BUY Alpaca rejects with "insufficient buying power". These
tests exercise the added guardrail that clamps (or skips) the SPY rebalance
BUY against BrokerClient.buying_power().

Pure-logic tests: no real BrokerClient is constructed, just a duck-typed fake
exposing the methods rebalance_to_spy() calls.
"""

from __future__ import annotations

from types import SimpleNamespace

from core import spy_base


class _FakeTradeClient:
    def __init__(self):
        self.submitted = []

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="order-1")


class _FakeBroker:
    def __init__(self, *, equity: float, buying_power: float, spy_price: float):
        self.trade = _FakeTradeClient()
        self._equity = equity
        self._buying_power = buying_power
        self._spy_price = spy_price

    def portfolio_value(self) -> float:
        return self._equity

    def cash(self) -> float:
        return self._equity

    def buying_power(self) -> float:
        return self._buying_power

    def get_positions(self) -> list:
        return []  # no PEAD/satellite positions held

    def get_position(self, symbol: str):
        return None  # no existing SPY position

    def get_price(self, symbol: str) -> float:
        return self._spy_price

    def get_open_orders(self):
        return []


def _patch_spy_config(monkeypatch):
    monkeypatch.setattr(spy_base.config, "SPY_BASE_ENABLED", True)
    monkeypatch.setattr(spy_base.config, "SPY_CASH_RESERVE_PCT", 0.10)
    monkeypatch.setattr(spy_base.config, "SPY_REBALANCE_BAND", 0.05)
    monkeypatch.setattr(spy_base.config, "SPY_MAX_PCT", 0.93)
    monkeypatch.setattr(spy_base, "send_trade_alert", lambda **kwargs: True)


def test_rebalance_buy_unaffected_when_cash_covers_order(monkeypatch):
    # equity=$100k, target diff ~$90k underweight; plenty of buying power.
    _patch_spy_config(monkeypatch)
    broker = _FakeBroker(equity=100_000.0, buying_power=100_000.0, spy_price=590.0)

    result = spy_base.rebalance_to_spy(broker)

    assert result["action"] == "buy"
    assert result["qty"] == 152  # int(90_000 / 590)
    assert broker.trade.submitted[-1].qty == 152


def test_rebalance_buy_clamped_when_order_exceeds_buying_power(monkeypatch):
    # Sized qty (152 sh, ~$89.7k) would exceed the $5k actually available.
    _patch_spy_config(monkeypatch)
    broker = _FakeBroker(equity=100_000.0, buying_power=5_000.0, spy_price=590.0)

    result = spy_base.rebalance_to_spy(broker)

    assert result["action"] == "buy"
    assert result["qty"] == 8  # int(5_000 / 590)
    assert broker.trade.submitted[-1].qty == 8


def test_rebalance_buy_skipped_when_cannot_afford_one_share(monkeypatch):
    # Regression: this is the exact shape of the live bug — 1 share forced
    # via max(1, ...) against buying power too small to afford it.
    _patch_spy_config(monkeypatch)
    broker = _FakeBroker(equity=100_000.0, buying_power=263.43, spy_price=590.0)

    result = spy_base.rebalance_to_spy(broker)

    assert result["action"] == "insufficient_cash"
    assert broker.trade.submitted == []
