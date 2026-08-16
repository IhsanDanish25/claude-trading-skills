"""Unit tests for hold.py -- Buffett Value Hold/Monitor Agent."""

import hold

from conftest import make_entry_snapshot


def test_monitor_position_thesis_intact_when_fundamentals_stable(monkeypatch):
    entry = make_entry_snapshot()
    current = make_entry_snapshot()  # unchanged fundamentals

    monkeypatch.setattr(hold, "analyze_symbol", lambda symbol: current)

    result = hold.monitor_position("AAPL", entry)

    assert result["thesis_intact"] is True
    assert result["fundamentals_deteriorated"] is False
    assert result["deterioration_reasons"] == []


def test_monitor_position_flags_roe_deterioration(monkeypatch):
    entry = make_entry_snapshot(roe_score=1.0)
    current = make_entry_snapshot(roe_score=0.5)  # 50% relative decline

    monkeypatch.setattr(hold, "analyze_symbol", lambda symbol: current)

    result = hold.monitor_position("AAPL", entry)

    assert result["fundamentals_deteriorated"] is True
    assert result["thesis_intact"] is False
    assert any("ROE" in reason for reason in result["deterioration_reasons"])


def test_monitor_position_flags_debt_spike(monkeypatch):
    entry = make_entry_snapshot(debt_score=0.9)
    current = make_entry_snapshot(debt_score=0.3)  # >=50% relative decline

    monkeypatch.setattr(hold, "analyze_symbol", lambda symbol: current)

    result = hold.monitor_position("AAPL", entry)

    assert result["fundamentals_deteriorated"] is True
    assert any("Debt/Equity" in reason for reason in result["deterioration_reasons"])


def test_monitor_position_does_not_flag_debt_already_at_zero_at_entry(monkeypatch):
    """Regression: entry_debt=0.0 with current_debt=0.0 is unchanged, not a decline.

    A zero-floor score has nowhere to fall to; 0.0 <= 0.0 * (1 - pct) is
    trivially true and must not be read as deterioration.
    """
    entry = make_entry_snapshot(debt_score=0.0)
    current = make_entry_snapshot(debt_score=0.0)  # identical, unchanged

    monkeypatch.setattr(hold, "analyze_symbol", lambda symbol: current)

    result = hold.monitor_position("AAPL", entry)

    assert not any("Debt/Equity" in reason for reason in result["deterioration_reasons"])


def test_monitor_position_flags_business_quality_screen_failure(monkeypatch):
    entry = make_entry_snapshot(business_quality_passed=True)
    current = make_entry_snapshot(business_quality_passed=False)

    monkeypatch.setattr(hold, "analyze_symbol", lambda symbol: current)

    result = hold.monitor_position("AAPL", entry)

    assert result["fundamentals_deteriorated"] is True
    assert any("no longer passes screen" in reason for reason in result["deterioration_reasons"])


def test_monitor_position_flags_margin_of_safety_gone(monkeypatch):
    entry = make_entry_snapshot(graham_ratio=1.4)
    current = make_entry_snapshot(graham_ratio=0.95)  # price now above fair value

    monkeypatch.setattr(hold, "analyze_symbol", lambda symbol: current)

    result = hold.monitor_position("AAPL", entry)

    assert result["fundamentals_deteriorated"] is True
    assert any("Margin of safety gone" in reason for reason in result["deterioration_reasons"])


def test_monitor_position_flags_margin_of_safety_erosion(monkeypatch):
    entry = make_entry_snapshot(graham_ratio=1.4)
    current = make_entry_snapshot(graham_ratio=1.15)  # ratio still >1 but eroded >=50%

    monkeypatch.setattr(hold, "analyze_symbol", lambda symbol: current)

    result = hold.monitor_position("AAPL", entry)

    assert result["fundamentals_deteriorated"] is True
    assert any("Margin of safety eroded" in reason for reason in result["deterioration_reasons"])


def test_monitor_position_handles_missing_current_data(monkeypatch):
    entry = make_entry_snapshot()

    monkeypatch.setattr(hold, "analyze_symbol", lambda symbol: None)

    result = hold.monitor_position("AAPL", entry)

    assert result["fundamentals_deteriorated"] is True
    assert result["current"] is None
    assert "data unavailable" in result["deterioration_reasons"][0]


def test_find_better_opportunity_returns_higher_conviction_candidate():
    pool = [
        {"symbol": "AAPL", "conviction_score": 0.8},
        {"symbol": "MSFT", "conviction_score": 0.97},
        {"symbol": "JNJ", "conviction_score": 0.82},
    ]

    better = hold.find_better_opportunity("AAPL", 0.8, pool)

    assert better is not None
    assert better["symbol"] == "MSFT"


def test_find_better_opportunity_returns_none_when_no_material_edge():
    pool = [
        {"symbol": "AAPL", "conviction_score": 0.8},
        {"symbol": "MSFT", "conviction_score": 0.85},  # only +0.05, below default 0.15 edge
    ]

    better = hold.find_better_opportunity("AAPL", 0.8, pool)

    assert better is None


def test_find_better_opportunity_excludes_self():
    pool = [{"symbol": "AAPL", "conviction_score": 0.99}]

    better = hold.find_better_opportunity("AAPL", 0.8, pool)

    assert better is None


def test_monitor_portfolio_has_no_time_based_exit_signal(monkeypatch):
    """Never exits on time-in-trade: no hold_days/days_held field anywhere in output."""
    entry = make_entry_snapshot()
    monkeypatch.setattr(hold, "analyze_symbol", lambda symbol: make_entry_snapshot())

    positions = [{"symbol": "AAPL", "entry_snapshot": entry}]
    results = hold.monitor_portfolio(positions)

    assert len(results) == 1
    for key in results[0]:
        assert "hold_day" not in key.lower()
        assert "days_held" not in key.lower()


def test_monitor_portfolio_attaches_better_opportunity(monkeypatch):
    entry = make_entry_snapshot(symbol="AAPL", conviction_score=0.7)
    monkeypatch.setattr(hold, "analyze_symbol", lambda symbol: make_entry_snapshot(symbol=symbol))

    positions = [{"symbol": "AAPL", "entry_snapshot": entry}]
    candidate_pool = [{"symbol": "MSFT", "conviction_score": 0.95}]

    results = hold.monitor_portfolio(positions, candidate_pool=candidate_pool)

    assert results[0]["better_opportunity"]["symbol"] == "MSFT"
