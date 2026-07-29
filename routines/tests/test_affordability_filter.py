"""Tests for the min-affordability candidate filter.

Screeners rank candidates by signal strength, not price. On a small account
(the live account this filter was added for runs at ~$268 equity), the
top-ranked candidate is frequently priced above what a single whole share
costs within the position cap — and PEAD's `candidates[:slots[0]]`
truncation happened BEFORE any affordability check, so an expensive top
candidate could crowd out a cheaper, buyable one further down the list.
_affordable_candidates() must run before that truncation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_open


def _make_log():
    return MagicMock()


def test_affordable_candidates_drops_unaffordable_and_keeps_affordable():
    broker = MagicMock()
    # AAPL costs more than its own affordable budget; MSFT does not.
    broker.affordable_budget.side_effect = lambda sym: {"AAPL": 20.0, "MSFT": 500.0}[sym]
    candidates = [
        {"symbol": "AAPL", "price": 340.0},
        {"symbol": "MSFT", "price": 400.0},
    ]

    result = market_open._affordable_candidates(broker, candidates, "pead", _make_log())

    assert [c["symbol"] for c in result] == ["MSFT"]


def test_affordable_candidates_drops_zero_or_missing_price():
    broker = MagicMock()
    broker.affordable_budget.return_value = 1000.0
    candidates = [{"symbol": "ZERO", "price": 0}, {"symbol": "NOPRICE"}]

    result = market_open._affordable_candidates(broker, candidates, "pead", _make_log())

    assert result == []


def test_run_pead_skips_unaffordable_top_candidate_and_buys_cheaper_one(monkeypatch):
    # Regression: with slots=[1] and candidates=[expensive, cheap], the old
    # candidates[:slots[0]] truncation tried ONLY the expensive one, got
    # blocked, and never reached the cheap one — zero buys despite an
    # affordable candidate existing. The filter must run first so the
    # truncated window contains only buyable names.
    monkeypatch.setattr(market_open, "_sector_gate", lambda *a, **k: True)
    monkeypatch.setattr(market_open, "free_cash_for_pead", lambda broker, amount: True)
    monkeypatch.setattr(market_open, "pead_track", lambda *a, **k: None)
    monkeypatch.setattr(market_open, "send_trade_alert", lambda **k: True)
    monkeypatch.setattr(market_open, "_mark_bought", lambda *a, **k: None)
    monkeypatch.setattr(market_open, "_append_trade_log", lambda *a, **k: None)
    monkeypatch.setattr(market_open.trade_logger, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(market_open, "rebalance_to_spy", lambda broker: {"action": "none"})
    monkeypatch.setattr(market_open, "spy_log", lambda broker: None)

    candidates = [
        {"symbol": "EXPENSIVE", "surprise_pct": 20.0, "report_date": "2026-07-28", "price": 340.0},
        {"symbol": "CHEAP", "surprise_pct": 12.0, "report_date": "2026-07-28", "price": 15.0},
    ]
    monkeypatch.setattr(market_open, "screen_earnings", lambda **k: candidates)

    broker = MagicMock()
    broker.affordable_budget.side_effect = lambda sym: {"EXPENSIVE": 21.44, "CHEAP": 21.44}[sym]
    broker.buy.return_value = {
        "qty": 1, "price": 15.0, "stop": 12.75, "stop_attached": True,
    }
    cb = MagicMock()
    slots = [1]  # only one open slot — old code would burn it on EXPENSIVE and stop

    market_open._run_pead(
        broker=broker, cb=cb, pv=268.03, slots=slots,
        held=set(), already_bought_today=set(), sector_counts={},
    )

    broker.buy.assert_called_once()
    assert broker.buy.call_args.args[0] == "CHEAP"
    assert slots == [0]
