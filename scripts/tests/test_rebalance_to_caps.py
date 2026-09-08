"""Unit tests for scripts.rebalance_to_caps.

Pure-logic tests: no live Alpaca access. A fake broker stands in for
BrokerClient. SPY base coverage guards against rebalance_to_caps treating
the spy_base cash-parking holding like an ordinary position — it must
never be counted against MAX_OPEN_POSITIONS / MAX_POSITION_SIZE_PCT, and
must never appear in survivors/closures/trims.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import config
from scripts.rebalance_to_caps import build_plan


def _pos(symbol, qty=10.0, market_value=1000.0, unrealized_pl=50.0, avg_entry_price=100.0):
    return SimpleNamespace(
        symbol=symbol, qty=qty, market_value=market_value,
        unrealized_pl=unrealized_pl, avg_entry_price=avg_entry_price,
    )


class _FakeBroker:
    def __init__(self, positions, equity, cash):
        self._positions = positions
        self._equity = equity
        self._cash = cash

    def get_positions(self):
        return self._positions

    def portfolio_value(self):
        return self._equity

    def cash(self):
        return self._cash


def test_spy_base_never_counted_toward_position_count_cap(monkeypatch):
    monkeypatch.setattr(config, "SPY_BASE_ENABLED", True)
    # SPY base dominates equity (70%) — well past any per-position cap —
    # plus exactly `target_positions` ordinary satellite positions.
    positions = [
        _pos("SPY", qty=100, market_value=70_000.0, avg_entry_price=700.0),
        _pos("AAA", qty=10, market_value=1_000.0),
        _pos("BBB", qty=10, market_value=1_000.0),
    ]
    broker = _FakeBroker(positions, equity=100_000.0, cash=28_000.0)

    plan = build_plan(broker, target_positions=2, max_pct=5.0, keep_symbols=None)

    assert plan["status"] == "within_caps"
    assert [r["symbol"] for r in plan["positions"]] == ["AAA", "BBB"]
    assert [r["symbol"] for r in plan["base_positions"]] == ["SPY"]


def test_spy_base_never_trimmed_or_closed_when_satellites_exceed_caps(monkeypatch):
    monkeypatch.setattr(config, "SPY_BASE_ENABLED", True)
    positions = [
        _pos("SPY", qty=100, market_value=70_000.0, avg_entry_price=700.0),
        _pos("AAA", qty=10, market_value=1_000.0, unrealized_pl=100.0),
        _pos("BBB", qty=10, market_value=1_000.0, unrealized_pl=-50.0),
        _pos("CCC", qty=10, market_value=1_000.0, unrealized_pl=10.0),
    ]
    broker = _FakeBroker(positions, equity=100_000.0, cash=27_000.0)

    plan = build_plan(broker, target_positions=2, max_pct=5.0, keep_symbols=None)

    assert plan["status"] == "needs_rebalance"
    all_symbols_touched = (
        {r["symbol"] for r in plan["survivors"]}
        | {r["symbol"] for r in plan["closures"]}
        | {t["symbol"] for t in plan["trims"]}
    )
    assert "SPY" not in all_symbols_touched
    assert [r["symbol"] for r in plan["base_positions"]] == ["SPY"]


def test_spy_base_exempt_from_per_position_pct_cap(monkeypatch):
    monkeypatch.setattr(config, "SPY_BASE_ENABLED", True)
    # SPY alone is 70% of equity, far over a 5% per-position cap, but is
    # the only position — so nothing else should be flagged as over-cap.
    positions = [_pos("SPY", qty=100, market_value=70_000.0, avg_entry_price=700.0)]
    broker = _FakeBroker(positions, equity=100_000.0, cash=30_000.0)

    plan = build_plan(broker, target_positions=10, max_pct=5.0, keep_symbols=None)

    assert plan["status"] == "empty"
    assert plan["message"] == "No open positions (excluding SPY base)."
    assert [r["symbol"] for r in plan["base_positions"]] == ["SPY"]


def test_keep_symbols_ignores_spy_base_without_error(monkeypatch):
    monkeypatch.setattr(config, "SPY_BASE_ENABLED", True)
    positions = [
        _pos("SPY", qty=100, market_value=70_000.0, avg_entry_price=700.0),
        _pos("AAA", qty=10, market_value=1_000.0),
        _pos("BBB", qty=10, market_value=1_000.0),
    ]
    broker = _FakeBroker(positions, equity=100_000.0, cash=28_000.0)

    plan = build_plan(broker, target_positions=1, max_pct=5.0, keep_symbols=["AAA", "SPY"])

    assert plan["status"] == "needs_rebalance"
    assert [r["symbol"] for r in plan["survivors"]] == ["AAA"]
    assert [r["symbol"] for r in plan["closures"]] == ["BBB"]
    assert [r["symbol"] for r in plan["base_positions"]] == ["SPY"]


def test_spy_not_exempt_when_base_disabled(monkeypatch):
    monkeypatch.setattr(config, "SPY_BASE_ENABLED", False)
    positions = [
        _pos("SPY", qty=100, market_value=70_000.0, avg_entry_price=700.0),
        _pos("AAA", qty=10, market_value=1_000.0),
    ]
    broker = _FakeBroker(positions, equity=100_000.0, cash=29_000.0)

    plan = build_plan(broker, target_positions=1, max_pct=5.0, keep_symbols=None)

    assert plan["status"] == "needs_rebalance"
    assert plan["base_positions"] == []
    # With the base exemption disabled, SPY is ranked/treated like any
    # other position and can end up closed or trimmed.
    all_symbols_touched = (
        {r["symbol"] for r in plan["survivors"]}
        | {r["symbol"] for r in plan["closures"]}
        | {t["symbol"] for t in plan["trims"]}
    )
    assert "SPY" in all_symbols_touched
