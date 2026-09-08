"""Unit tests for scripts.close_strategy_positions.

Pure-logic tests: no live Alpaca access. A fake broker + in-memory tracker
stand in for BrokerClient / core.pead_tracker.
"""

from __future__ import annotations

from types import SimpleNamespace

from scripts.close_strategy_positions import build_plan, execute_plan


def _pos(symbol, qty=10, market_value=1000.0, unrealized_pl=50.0):
    return SimpleNamespace(symbol=symbol, qty=qty, market_value=market_value,
                            unrealized_pl=unrealized_pl)


def _order(id_, symbol, side="sell"):
    return SimpleNamespace(id=id_, symbol=symbol, side=side)


class _FakeTradeClient:
    def __init__(self):
        self.cancelled_ids = []

    def cancel_order_by_id(self, order_id):
        self.cancelled_ids.append(order_id)


class _FakeBroker:
    def __init__(self, positions=None, open_orders=None):
        self.trade = _FakeTradeClient()
        self._positions = positions or []
        self._open_orders = open_orders or []
        self.closed = []

    def get_positions(self):
        return self._positions

    def get_open_orders(self):
        return self._open_orders

    def close_position(self, symbol):
        self.closed.append(symbol)


def test_build_plan_buckets_positions_correctly():
    broker = _FakeBroker(positions=[_pos("ABCD"), _pos("SPY")])
    tracker = {
        "ABCD": {"strategy": "breakout", "entry_date": "2026-06-01"},
        "ZOMB": {"strategy": "earnmom", "entry_date": "2026-05-01"},  # no longer open
        "SPY":  {"strategy": "pead", "entry_date": "2026-06-15"},     # different strategy, kept
    }

    plan = build_plan(broker, tracker, ["breakout", "earnmom"])

    assert [r["symbol"] for r in plan["to_close"]] == ["ABCD"]
    assert [r["symbol"] for r in plan["stale_tracker"]] == ["ZOMB"]
    assert plan["untracked_positions"] == []


def test_build_plan_flags_untracked_open_positions():
    broker = _FakeBroker(positions=[_pos("MYST")])
    plan = build_plan(broker, tracker={}, strategies=["breakout", "earnmom"])

    assert plan["to_close"] == []
    assert plan["stale_tracker"] == []
    assert [r["symbol"] for r in plan["untracked_positions"]] == ["MYST"]


def test_execute_plan_cancels_stale_orders_then_closes_and_untracks():
    broker = _FakeBroker(
        positions=[_pos("ABCD")],
        open_orders=[_order("stale-1", "ABCD"), _order("other-sym", "SPY")],
    )
    tracker = {"ABCD": {"strategy": "breakout", "entry_date": "2026-06-01"}}
    plan = build_plan(broker, tracker, ["breakout", "earnmom"])

    removed = []
    ok = execute_plan(broker, plan, remove_fn=removed.append)

    assert ok is True
    assert broker.trade.cancelled_ids == ["stale-1"]
    assert broker.closed == ["ABCD"]
    assert removed == ["ABCD"]


def test_execute_plan_untracks_stale_entries_without_touching_broker():
    broker = _FakeBroker(positions=[])
    tracker = {"ZOMB": {"strategy": "earnmom", "entry_date": "2026-05-01"}}
    plan = build_plan(broker, tracker, ["breakout", "earnmom"])

    removed = []
    ok = execute_plan(broker, plan, remove_fn=removed.append)

    assert ok is True
    assert broker.closed == []
    assert removed == ["ZOMB"]


def test_execute_plan_reports_failure_but_continues():
    broker = _FakeBroker(positions=[_pos("ABCD"), _pos("EFGH")])

    def _raise(symbol):
        raise RuntimeError(f"boom {symbol}")

    broker.close_position = _raise
    tracker = {
        "ABCD": {"strategy": "breakout", "entry_date": "2026-06-01"},
        "EFGH": {"strategy": "earnmom", "entry_date": "2026-06-01"},
    }
    plan = build_plan(broker, tracker, ["breakout", "earnmom"])

    removed = []
    ok = execute_plan(broker, plan, remove_fn=removed.append)

    assert ok is False
    assert removed == []
