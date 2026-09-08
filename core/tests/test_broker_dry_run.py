"""Unit tests for DRY_RUN — buy() short-circuits right before order
submission when core.broker.DRY_RUN is True.

The paper trading account no longer exists (deleted 2026-08-01), so DRY_RUN
is the only way to validate the full live pipeline (spread gate, sizing,
cost tracking) against real market data without risking real capital. These
tests assert: no order is ever submitted while DRY_RUN is on, the spread
gate still runs first (a wide spread still blocks in dry-run), and
cost_tracker still records a (zero-slippage-by-construction) fill so the
logging pipeline itself gets exercised.

Pure-logic tests: BrokerClient built via object.__new__, mirroring the
pattern in test_broker_buy_cash_cap.py / test_broker_spread_check.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import core.broker as broker_module
from core.broker import BrokerClient


def _quote(bid, ask):
    return SimpleNamespace(bid_price=bid, ask_price=ask)


class _ExplodingTradeClient:
    """submit_order must never be called while DRY_RUN is on."""

    def submit_order(self, req):
        raise AssertionError("submit_order should not be called in DRY_RUN mode")


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


def test_dry_run_buy_never_submits_order(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    broker = _make_broker(ref_price=100.0, equity=100_000.0, buying_power=100_000.0)

    result = broker.buy("AAPL", shares=10, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result["dry_run"] is True
    assert result.get("blocked") is None
    assert result["qty"] == 10
    assert result["price"] == 100.0
    assert result["stop"] == 95.0
    assert result["target"] == 110.0


def test_dry_run_blocks_rather_than_going_fractional(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    # ref_price high enough / cash low enough that a whole share doesn't
    # fit — fractional buys are disabled, so this blocks even in DRY_RUN
    # (the block check runs before the DRY_RUN short-circuit).
    broker = _make_broker(ref_price=339.0, equity=264.0, buying_power=205.0)

    result = broker.buy("AAPL", dollar_amount=26.0, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result == {"blocked": True, "reason": "insufficient_cash"}


def test_dry_run_still_enforces_spread_gate(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    broker = object.__new__(BrokerClient)
    broker.trade = _ExplodingTradeClient()
    broker.is_market_open = lambda: True
    broker.get_latest_quotes = lambda symbols: {symbols[0]: _quote(90.0, 110.0)}  # 20% spread

    result = broker.buy("AAPL", shares=10)

    assert result == {"blocked": True, "reason": "spread_too_wide", "spread_pct": 0.2}


def test_dry_run_records_cost_tracker_fill(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    broker = _make_broker(ref_price=50.0, equity=100_000.0, buying_power=100_000.0)

    recorded = {}

    def _fake_record_fill(**kwargs):
        recorded.update(kwargs)
        return kwargs

    monkeypatch.setattr(broker_module.cost_tracker, "record_fill", _fake_record_fill)
    monkeypatch.setattr(broker_module.trade_logger, "log_event", lambda *a, **k: None)

    result = broker.buy("AAPL", shares=4, strategy="breakout")

    assert result["dry_run"] is True
    assert recorded["strategy"] == "breakout"
    assert recorded["symbol"] == "AAPL"
    assert recorded["signal_price"] == recorded["fill_price"] == 50.0
    assert recorded["qty"] == 4


def test_dry_run_logs_intended_order(monkeypatch):
    monkeypatch.setattr(broker_module, "DRY_RUN", True)
    broker = _make_broker(ref_price=50.0, equity=100_000.0, buying_power=100_000.0)

    calls = []

    def _fake_log_event(event_type, strategy, symbol=None, **fields):
        calls.append({"event_type": event_type, "strategy": strategy, "symbol": symbol, **fields})

    monkeypatch.setattr(broker_module.trade_logger, "log_event", _fake_log_event)

    broker.buy("AAPL", shares=4, strategy="meanrev")

    # cost_tracker.record_fill logs its own event too (event="cost_tracking")
    # -- the dry-run order intent is a separate, earlier call.
    dry_run_calls = [c for c in calls if c["event_type"] == "dry_run_order"]
    assert len(dry_run_calls) == 1
    logged = dry_run_calls[0]
    assert logged["strategy"] == "meanrev"
    assert logged["symbol"] == "AAPL"
    assert logged["side"] == "buy"
    assert logged["qty"] == 4


def test_dry_run_off_still_submits_real_order(monkeypatch):
    # Sanity check the flag is a real gate — default (DRY_RUN unset/False)
    # behaves exactly like before this change.
    monkeypatch.setattr(broker_module, "DRY_RUN", False)
    broker = object.__new__(BrokerClient)

    class _FakeTradeClient:
        def __init__(self):
            self.submitted = []

        def submit_order(self, req):
            self.submitted.append(req)
            return SimpleNamespace(id="order-1")

        def get_order_by_id(self, order_id):
            return SimpleNamespace(filled_avg_price=50.0, filled_qty=4)

    broker.trade = _FakeTradeClient()
    broker.is_market_open = lambda: True
    broker.get_latest_quotes = lambda symbols: {symbols[0]: _quote(49.9, 50.1)}
    broker.get_price = lambda symbol: 50.0
    broker.portfolio_value = lambda: 100_000.0
    broker.buying_power = lambda: 100_000.0
    broker.get_position = lambda symbol: None
    broker.position_count = lambda: 0
    broker.attach_stop_target = lambda symbol, qty, stop, target: (True, True)

    result = broker.buy("AAPL", shares=4)

    assert result.get("dry_run") is None
    assert len(broker.trade.submitted) == 1
