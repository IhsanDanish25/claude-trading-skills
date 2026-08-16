"""Unit tests for BrokerClient.buy_simple() / sell_limit() -- the stop-less
order paths added for Buffett Value (see skills/buffett-value/scripts/,
which explicitly forbids a stop-loss and calendar exit).

Pure-logic tests: BrokerClient built via object.__new__, mirroring the
pattern in test_broker_dry_run.py. The paper trading account no longer
exists (deleted 2026-08-01), so DRY_RUN is the only way to exercise the
full pipeline without risking real capital.
"""
from __future__ import annotations

from types import SimpleNamespace

import core.broker as broker_module
from alpaca.trading.enums import OrderClass
from core.broker import BrokerClient


def _quote(bid, ask):
    return SimpleNamespace(bid_price=bid, ask_price=ask)


class _ExplodingTradeClient:
    def submit_order(self, req):
        raise AssertionError("submit_order should not be called")


def _make_broker(*, ref_price: float, equity: float, buying_power: float):
    broker = object.__new__(BrokerClient)
    broker.trade = _ExplodingTradeClient()
    broker.is_market_open = lambda: True
    broker.get_latest_quotes = lambda symbols: {
        symbols[0]: _quote(ref_price * 0.999, ref_price * 1.001)
    }
    broker.get_price = lambda symbol: ref_price
    broker.portfolio_value = lambda: equity
    broker.buying_power = lambda: buying_power
    broker.get_position = lambda symbol: None
    broker.position_count = lambda: 0
    return broker


# ── buy_simple ────────────────────────────────────────────────────────────


def test_buy_simple_dry_run_never_submits_order(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    broker = _make_broker(ref_price=100.0, equity=100_000.0, buying_power=100_000.0)

    result = broker.buy_simple("CMCSA", dollar_amount=5_000.0, strategy="buffett_value")

    assert result["dry_run"] is True
    assert result.get("blocked") is None
    assert result["qty"] == 50  # $5,000 / $100
    assert result["price"] == 100.0


def test_buy_simple_never_carries_a_stop_or_target(monkeypatch):
    """Regression: buy_simple's result must never imply a stop was attached."""
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    broker = _make_broker(ref_price=100.0, equity=100_000.0, buying_power=100_000.0)

    result = broker.buy_simple("CMCSA", dollar_amount=5_000.0)

    assert "stop" not in result
    assert "target" not in result
    assert "stop_attached" not in result


def test_buy_simple_still_enforces_spread_gate(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    broker = object.__new__(BrokerClient)
    broker.trade = _ExplodingTradeClient()
    broker.is_market_open = lambda: True
    broker.get_latest_quotes = lambda symbols: {symbols[0]: _quote(90.0, 110.0)}  # 20% spread

    result = broker.buy_simple("CMCSA", dollar_amount=5_000.0)

    assert result == {"blocked": True, "reason": "spread_too_wide", "spread_pct": 0.2}


def test_buy_simple_blocks_at_max_open_positions(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    broker = _make_broker(ref_price=100.0, equity=100_000.0, buying_power=100_000.0)
    broker.position_count = lambda: 999  # force at/above MAX_OPEN_POSITIONS

    result = broker.buy_simple("CMCSA", dollar_amount=5_000.0)

    assert result == {"blocked": True, "reason": "max_open_positions"}


def test_buy_simple_clamps_to_buying_power(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    broker = _make_broker(ref_price=100.0, equity=100_000.0, buying_power=250.0)

    result = broker.buy_simple("CMCSA", dollar_amount=5_000.0)

    assert result["qty"] == 2  # floor(250 / 100)


def test_buy_simple_blocks_when_cannot_afford_one_share(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    broker = _make_broker(ref_price=100.0, equity=100_000.0, buying_power=50.0)

    result = broker.buy_simple("CMCSA", dollar_amount=5_000.0)

    assert result == {"blocked": True, "reason": "insufficient_cash"}


def test_buy_simple_dry_run_off_submits_plain_market_order_no_stop_attach(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", False)
    broker = object.__new__(BrokerClient)

    class _FakeTradeClient:
        def __init__(self):
            self.submitted = []

        def submit_order(self, req):
            self.submitted.append(req)
            return SimpleNamespace(id="order-1")

        def get_order_by_id(self, order_id):
            return SimpleNamespace(filled_avg_price=100.0, filled_qty=50)

    broker.trade = _FakeTradeClient()
    broker.is_market_open = lambda: True
    broker.get_latest_quotes = lambda symbols: {symbols[0]: _quote(99.9, 100.1)}
    broker.get_price = lambda symbol: 100.0
    broker.portfolio_value = lambda: 100_000.0
    broker.buying_power = lambda: 100_000.0
    broker.get_position = lambda symbol: None
    broker.position_count = lambda: 0

    def _exploding_attach(*a, **k):
        raise AssertionError("attach_stop_target must never be called by buy_simple")

    broker.attach_stop_target = _exploding_attach

    result = broker.buy_simple("CMCSA", dollar_amount=5_000.0, strategy="buffett_value")

    assert len(broker.trade.submitted) == 1
    assert result["qty"] == 50
    assert result["price"] == 100.0
    assert "stop" not in result


def test_buy_simple_does_not_log_cost_tracker_fill_when_unconfirmed(monkeypatch):
    """Regression: buy() only logs to cost_tracker when fill_price is not
    None (core/broker.py ~line 780) -- buy_simple() was missing that guard
    and would log a synthetic zero-slippage fill at ref_price for an order
    that never actually confirmed filled within the poll window."""
    monkeypatch.setattr(broker_module, "DRY_RUN", False)
    monkeypatch.setattr(broker_module.time, "sleep", lambda s: None)
    broker = object.__new__(BrokerClient)

    class _FakeTradeClient:
        def submit_order(self, req):
            return SimpleNamespace(id="order-5")

        def get_order_by_id(self, order_id):
            return SimpleNamespace(filled_avg_price=None, filled_qty=None)  # never confirms

    broker.trade = _FakeTradeClient()
    broker.is_market_open = lambda: True
    broker.get_latest_quotes = lambda symbols: {symbols[0]: _quote(99.9, 100.1)}
    broker.get_price = lambda symbol: 100.0
    broker.portfolio_value = lambda: 100_000.0
    broker.buying_power = lambda: 100_000.0
    broker.get_position = lambda symbol: None
    broker.position_count = lambda: 0

    recorded = []
    monkeypatch.setattr(broker_module.cost_tracker, "record_fill", lambda **k: recorded.append(k))

    broker.buy_simple("CMCSA", dollar_amount=5_000.0, strategy="buffett_value")

    assert recorded == []


# ── sell_limit ────────────────────────────────────────────────────────────


def test_sell_limit_dry_run_never_submits_order(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    broker = object.__new__(BrokerClient)
    broker.trade = _ExplodingTradeClient()

    result = broker.sell_limit("CMCSA", qty=50, limit_price=42.53)

    assert result["dry_run"] is True
    assert result["qty"] == 50
    assert result["limit_price"] == 42.53


def test_sell_limit_submits_simple_limit_order_never_bracket(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", False)
    monkeypatch.setattr(broker_module.time, "sleep", lambda s: None)  # skip real delay
    broker = object.__new__(BrokerClient)

    class _FakeTradeClient:
        def __init__(self):
            self.submitted = []

        def submit_order(self, req):
            self.submitted.append(req)
            return SimpleNamespace(id="order-2")

        def get_order_by_id(self, order_id):
            return SimpleNamespace(filled_avg_price=None, filled_qty=None)  # never confirms

    broker.trade = _FakeTradeClient()

    result = broker.sell_limit("CMCSA", qty=50, limit_price=42.53)

    assert len(broker.trade.submitted) == 1
    req = broker.trade.submitted[0]
    assert req.order_class == OrderClass.SIMPLE
    assert req.order_class != OrderClass.OCO
    assert float(req.limit_price) == 42.53
    assert result["qty"] == 50


def test_sell_limit_reports_unconfirmed_when_never_fills(monkeypatch):
    """Regression: sell_limit must never claim success on a resting/expired
    order -- callers rely on this flag to decide whether to alert/untrack."""
    monkeypatch.setattr(broker_module, "DRY_RUN", False)
    broker = object.__new__(BrokerClient)

    class _FakeTradeClient:
        def submit_order(self, req):
            return SimpleNamespace(id="order-3")

        def get_order_by_id(self, order_id):
            return SimpleNamespace(filled_avg_price=None, filled_qty=None)

    broker.trade = _FakeTradeClient()
    monkeypatch.setattr(broker_module.time, "sleep", lambda s: None)  # skip real delay

    result = broker.sell_limit("CMCSA", qty=50, limit_price=42.53)

    assert result["filled"] is False
    assert result["filled_qty"] is None
    assert result["filled_avg_price"] is None


def test_sell_limit_confirms_fill_when_order_fills(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", False)
    broker = object.__new__(BrokerClient)

    class _FakeTradeClient:
        def submit_order(self, req):
            return SimpleNamespace(id="order-4")

        def get_order_by_id(self, order_id):
            return SimpleNamespace(filled_avg_price=42.50, filled_qty=50)

    broker.trade = _FakeTradeClient()

    result = broker.sell_limit("CMCSA", qty=50, limit_price=42.53)

    assert result["filled"] is True
    assert result["filled_qty"] == 50
    assert result["filled_avg_price"] == 42.50
