"""Unit tests for BrokerClient.affordable_budget().

Strategy screeners rank candidates by signal strength, not price. On a small
account, the top-ranked candidate is often unaffordable (whole-share policy,
no fractional buys), and truncating the candidate list to the number of open
slots *before* checking affordability meant cheaper, affordable candidates
further down the list never got a chance. affordable_budget() gives callers
a cheap way to pre-filter candidates by price before attempting buy().
"""

from __future__ import annotations

from core.broker import BrokerClient


def _make_broker(*, equity: float, buying_power: float, existing_position=None) -> BrokerClient:
    broker = object.__new__(BrokerClient)
    broker.portfolio_value = lambda: equity
    broker.buying_power = lambda: buying_power
    broker.get_position = lambda symbol: existing_position
    return broker


def test_affordable_budget_is_position_cap_when_cash_is_plentiful(monkeypatch):
    monkeypatch.setattr("core.broker.MAX_POSITION_SIZE_PCT", 0.08)
    broker = _make_broker(equity=100_000.0, buying_power=100_000.0)

    assert broker.affordable_budget("AAPL") == 8_000.0


def test_affordable_budget_is_clamped_by_buying_power(monkeypatch):
    monkeypatch.setattr("core.broker.MAX_POSITION_SIZE_PCT", 0.08)
    broker = _make_broker(equity=100_000.0, buying_power=50.0)

    assert broker.affordable_budget("AAPL") == 50.0


def test_affordable_budget_nets_out_existing_position_value(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("core.broker.MAX_POSITION_SIZE_PCT", 0.08)
    existing = SimpleNamespace(market_value="6000.0")
    broker = _make_broker(equity=100_000.0, buying_power=100_000.0, existing_position=existing)

    # cap = 8000, already holding 6000 -> 2000 left
    assert broker.affordable_budget("AAPL") == 2_000.0


def test_affordable_budget_small_account_matches_live_268_equity(monkeypatch):
    # Regression: $268 account, 8% cap ~= $21.44 — a single $340 AAPL share
    # is unaffordable, which is exactly the scenario this method exists to
    # let callers detect before wasting a buy() attempt.
    monkeypatch.setattr("core.broker.MAX_POSITION_SIZE_PCT", 0.08)
    broker = _make_broker(equity=268.03, buying_power=268.03)

    budget = broker.affordable_budget("AAPL")
    assert round(budget, 2) == 21.44
    assert budget < 340.08  # can't afford 1 share of AAPL @ $340.08
