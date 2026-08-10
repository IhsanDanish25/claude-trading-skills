"""Tests for midday_review's scan-buy score-quality gate.

routines/midday_review.py hardcoded `score >= 75` in its buy-candidate
filter — on 2026-08-10 midday_review found 6 real VCP candidates but none
scored >=75, so nothing bought despite live setups existing. Lowered the
bar to config.MIDDAY_BUY_SCORE_MIN (default 70, env-overridable) so
marginally-strong setups can qualify. Extracted from an inline literal to
a named config constant for testability and consistency with the repo's
other env-overridable trading params (VCP_STOP_PCT, MAX_POSITION_SIZE_PCT).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import midday_review
from core import config


def _run_midday_scan(monkeypatch, tmp_path, *, candidate_score: int):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIDDAY_BUY_SCORE_MIN", 70.0)
    monkeypatch.setattr(midday_review.config, "validate", lambda: None)
    monkeypatch.setattr(midday_review, "_load_pyramided", lambda: set())
    monkeypatch.setattr(midday_review, "_save_pyramided", lambda symbols: None)
    monkeypatch.setattr(midday_review.time, "sleep", lambda *_: None)
    monkeypatch.setattr(midday_review, "is_base_symbol", lambda sym: False)
    monkeypatch.setattr(midday_review, "get_market_breadth", lambda: {})
    monkeypatch.setattr(
        midday_review, "analyze_market_regime",
        lambda breadth: {"regime": "neutral", "trade_bias": "moderate"},
    )
    monkeypatch.setattr(midday_review, "get_quotes", lambda symbols: {})
    monkeypatch.setattr(midday_review, "review_open_positions", lambda pos_data, regime: [])
    monkeypatch.setattr(midday_review, "spy_log", lambda broker: None)
    monkeypatch.setattr(midday_review, "rebalance_to_spy", lambda broker: {"action": "none"})
    monkeypatch.setattr(midday_review, "screen", lambda: [{"symbol": "PANW", "price": 200.0}])
    monkeypatch.setattr(
        midday_review, "score_vcp_candidates",
        lambda top: [{"symbol": "PANW", "score": candidate_score,
                      "reason": "tight VCP", "action": "BUY"}],
    )
    monkeypatch.setattr(midday_review, "_load_today_bought", lambda: set())
    monkeypatch.setattr(midday_review, "_mark_bought", lambda sym: None)
    monkeypatch.setattr(midday_review, "send_trade_alert", MagicMock())

    broker = MagicMock()
    broker.is_market_open.return_value = True
    broker.portfolio_value.return_value = 100_000.0
    broker.position_count.return_value = 0
    broker.get_account.return_value = MagicMock(equity=100_000.0)
    broker.get_positions.return_value = []
    broker.get_open_orders.return_value = []
    broker.affordable_budget.return_value = 100_000.0
    broker.buy.return_value = {
        "qty": 10, "price": 200.0, "stop": 184.0, "target": 220.0,
        "stop_attached": True,
    }
    monkeypatch.setattr(midday_review, "BrokerClient", lambda: broker)

    midday_review.run()
    return broker


def test_default_threshold_is_70_not_75():
    assert config.MIDDAY_BUY_SCORE_MIN == 70.0


def test_score_72_now_qualifies_below_old_75_bar(monkeypatch, tmp_path):
    """Regression: 2026-08-10 had real candidates that scored below the old
    75 bar and bought nothing. A 72 must now clear the (lowered) gate."""
    broker = _run_midday_scan(monkeypatch, tmp_path, candidate_score=72)
    broker.buy.assert_called_once_with("PANW", dollar_amount=100_000.0 * 0.08 * 0.5)


def test_score_69_still_rejected_below_new_70_bar(monkeypatch, tmp_path):
    """The gate is lowered, not removed — still-weak setups must not buy."""
    broker = _run_midday_scan(monkeypatch, tmp_path, candidate_score=69)
    broker.buy.assert_not_called()
