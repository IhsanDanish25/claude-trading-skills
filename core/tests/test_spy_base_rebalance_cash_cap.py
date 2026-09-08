"""Unit tests for rebalance_to_spy()'s BUY-branch buying-power guardrail.

rebalance_to_spy() sizes its BUY branch off the equity-vs-target dollar diff
alone and submits a notional (dollar-amount) order straight to
broker.trade.submit_order(), never checking BrokerClient.buying_power() —
unlike BrokerClient.buy(), which has that exact guardrail
(core/tests/test_broker_buy_cash_cap.py). On an already-invested account,
buying_power can be far smaller than the equity-sized diff, so this path
could submit a BUY Alpaca rejects with "insufficient buying power".

History: a buying-power clamp was added in 41befa3, then silently dropped
two days later by a388d9b when the BUY branch was refactored from whole-share
qty to notional/fractional ordering (a separate, legitimate fix for accounts
too small to afford one SPY share) — the refactor didn't carry the guard
forward. These tests exercise the guard re-added against the current notional
model: clamp the notional to available buying power, or skip entirely if
buying power can't cover even the $1 minimum notional Alpaca accepts.

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
    assert result["notional"] == 90_000.0
    assert result["qty"] == round(90_000.0 / 590.0, 4)
    assert broker.trade.submitted[-1].notional == 90_000.0


def test_rebalance_buy_clamped_when_order_exceeds_buying_power(monkeypatch):
    # Sized notional (~$90k) would exceed the $5k actually available.
    _patch_spy_config(monkeypatch)
    broker = _FakeBroker(equity=100_000.0, buying_power=5_000.0, spy_price=590.0)

    result = spy_base.rebalance_to_spy(broker)

    assert result["action"] == "buy"
    assert result["notional"] == 5_000.0
    assert result["qty"] == round(5_000.0 / 590.0, 4)
    assert broker.trade.submitted[-1].notional == 5_000.0


def test_rebalance_buy_uses_full_cash_when_below_share_price(monkeypatch):
    # $263.43 can't afford a whole SPY share (~$590), but notional/fractional
    # ordering means it's still a perfectly valid buy — this is exactly the
    # account size a388d9b's notional refactor was meant to support. Must NOT
    # be treated as insufficient cash.
    _patch_spy_config(monkeypatch)
    broker = _FakeBroker(equity=100_000.0, buying_power=263.43, spy_price=590.0)

    result = spy_base.rebalance_to_spy(broker)

    assert result["action"] == "buy"
    assert result["notional"] == 263.43
    assert broker.trade.submitted[-1].notional == 263.43


def test_rebalance_buy_skipped_when_below_min_notional(monkeypatch):
    # Regression: buying power too small for even Alpaca's $1 minimum
    # notional must skip cleanly instead of submitting an order Alpaca
    # will reject.
    _patch_spy_config(monkeypatch)
    broker = _FakeBroker(equity=100_000.0, buying_power=0.50, spy_price=590.0)

    result = spy_base.rebalance_to_spy(broker)

    assert result["action"] == "insufficient_cash"
    assert broker.trade.submitted == []
