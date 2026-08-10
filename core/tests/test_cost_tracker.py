"""Unit tests for core/cost_tracker.py -- per-trade slippage/cost logging
and the rolling 30d realized-vs-backtested edge report.

Logging is redirected to a temp STATE_DIR (monkeypatch) so tests never
touch the real state/cost_log.jsonl. trade_logger's Axiom path degrades to
jsonl-only automatically when AXIOM_API_TOKEN is unset (no network in test
env), so no mocking needed there.
"""

from __future__ import annotations

import json

import pytest

from core import cost_tracker


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cost_tracker, "STATE_DIR", str(tmp_path))
    yield


class TestRecordFill:
    def test_writes_one_line_with_correct_slippage(self):
        cost_tracker.record_fill(
            strategy="breakout",
            symbol="AAPL",
            side="buy",
            signal_price=100.0,
            fill_price=100.50,
            qty=10,
            spread_pct=0.004,
        )
        path = cost_tracker._cost_log_path()
        with open(path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert len(lines) == 1
        rec = lines[0]
        assert rec["strategy"] == "breakout"
        assert rec["symbol"] == "AAPL"
        assert rec["slippage_pct"] == pytest.approx(0.005, abs=1e-6)

    def test_sell_slippage_sign_flipped(self):
        # A sell filled BELOW signal price is the adverse direction for a
        # sell -- slippage_pct must be positive (a cost), matching buy's
        # convention of "positive = cost."
        cost_tracker.record_fill(
            strategy="breakout",
            symbol="AAPL",
            side="sell",
            signal_price=100.0,
            fill_price=99.0,
            qty=10,
        )
        with open(cost_tracker._cost_log_path()) as f:
            rec = json.loads(f.readline())
        assert rec["slippage_pct"] == pytest.approx(0.01, abs=1e-6)

    def test_missing_signal_price_does_not_raise_or_write(self):
        # Fail-safe: bad input must not crash the caller (a live buy/sell
        # that already executed) and must not write a garbage record.
        cost_tracker.record_fill(
            strategy="breakout",
            symbol="AAPL",
            side="buy",
            signal_price=0.0,
            fill_price=100.0,
            qty=10,
        )
        cost_tracker.record_fill(
            strategy="breakout",
            symbol="AAPL",
            side="buy",
            signal_price=None,
            fill_price=100.0,
            qty=10,
        )
        import os

        assert not os.path.exists(cost_tracker._cost_log_path())

    def test_missing_fill_price_does_not_raise(self):
        # Must not raise even on garbage input -- never block the trading path.
        cost_tracker.record_fill(
            strategy="breakout",
            symbol="AAPL",
            side="buy",
            signal_price=100.0,
            fill_price=None,
            qty=10,
        )


class TestComputeRollingEdge:
    def test_no_log_file_returns_zero_trades(self):
        result = cost_tracker.compute_rolling_edge("breakout", days=30)
        assert result["n_trades"] == 0
        assert result["avg_slippage_pct"] is None

    def test_averages_slippage_for_matching_strategy(self):
        cost_tracker.record_fill(
            strategy="breakout",
            symbol="AAPL",
            side="buy",
            signal_price=100.0,
            fill_price=101.0,
            qty=10,
        )
        cost_tracker.record_fill(
            strategy="breakout",
            symbol="MSFT",
            side="buy",
            signal_price=200.0,
            fill_price=198.0,
            qty=5,
        )
        # Different strategy -- must not be included in breakout's average.
        cost_tracker.record_fill(
            strategy="meanrev",
            symbol="NKE",
            side="buy",
            signal_price=50.0,
            fill_price=55.0,
            qty=1,
        )
        result = cost_tracker.compute_rolling_edge("breakout", days=30)
        assert result["n_trades"] == 2
        # (0.01 + -0.01) / 2 == 0.0
        assert result["avg_slippage_pct"] == pytest.approx(0.0, abs=1e-6)

    def test_ignores_malformed_lines(self, tmp_path):
        path = cost_tracker._cost_log_path()
        with open(path, "w") as f:
            f.write("not valid json\n")
            f.write(json.dumps({"strategy": "breakout"}) + "\n")  # missing _time
        result = cost_tracker.compute_rolling_edge("breakout", days=30)
        assert result["n_trades"] == 0  # both lines skipped, no crash


class TestWeeklyEdgeReport:
    def test_reports_backtested_sharpe_and_alert_flag(self):
        # avg_slippage 0.03 > EDGE_ALERT_DELTA_PCT (0.02) -> alert=True
        cost_tracker.record_fill(
            strategy="breakout",
            symbol="AAPL",
            side="buy",
            signal_price=100.0,
            fill_price=103.0,
            qty=10,
        )
        reports = cost_tracker.weekly_edge_report(["breakout", "meanrev"], days=30)
        by_strategy = {r["strategy"]: r for r in reports}

        assert by_strategy["breakout"]["n_trades"] == 1
        assert by_strategy["breakout"]["backtested_sharpe"] == 1.08
        assert by_strategy["breakout"]["alert"] is True

        assert by_strategy["meanrev"]["n_trades"] == 0
        assert by_strategy["meanrev"]["alert"] is False

    def test_unknown_strategy_does_not_raise(self):
        # e.g. a stray "pead" fill after PEAD was dropped from STRATEGY_MODES
        reports = cost_tracker.weekly_edge_report(["nonexistent_strategy"], days=30)
        assert reports[0]["backtested_sharpe"] is None
