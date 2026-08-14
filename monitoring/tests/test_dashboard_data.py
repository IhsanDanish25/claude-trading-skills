"""Unit tests for monitoring/dashboard.py's on-disk state loaders.

Only the pure data-loading functions are covered here — load_account() talks
to Alpaca via core.broker.BrokerClient and render_*() calls Streamlit chrome,
neither of which is meaningful to unit test. STATE_DIR is redirected to a
temp dir (monkeypatch) so tests never touch the real state/ directory.
"""

from __future__ import annotations

import json

import pytest

import core.config as config
from monitoring import dashboard


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    yield tmp_path


class TestLoadDayStartValue:
    def test_missing_file_returns_none_none(self):
        assert dashboard.load_day_start_value() == (None, None)

    def test_reads_value_and_date(self, tmp_path):
        (tmp_path / "day_start_value.json").write_text(
            json.dumps({"value": 100000.0, "date": "2026-08-12"})
        )
        assert dashboard.load_day_start_value() == (100000.0, "2026-08-12")

    def test_corrupt_json_returns_none_none(self, tmp_path):
        (tmp_path / "day_start_value.json").write_text("not json")
        assert dashboard.load_day_start_value() == (None, None)


class TestLoadSkippedRoutines:
    def test_missing_file_returns_empty_default(self):
        assert dashboard.load_skipped_routines() == {"date": None, "skipped": []}

    def test_reads_skipped_entries(self, tmp_path):
        payload = {"date": "2026-08-12", "skipped": [{"module": "midday_review", "reason": "x"}]}
        (tmp_path / "skipped_routines.json").write_text(json.dumps(payload))
        assert dashboard.load_skipped_routines() == payload

    def test_corrupt_json_returns_empty_default(self, tmp_path):
        (tmp_path / "skipped_routines.json").write_text("not json")
        assert dashboard.load_skipped_routines() == {"date": None, "skipped": []}


class TestLoadTradeLog:
    def test_missing_file_returns_empty_dataframe(self):
        df = dashboard.load_trade_log()
        assert df.empty

    def test_reads_and_sorts_by_ts(self, tmp_path):
        lines = [
            {"ts": "2026-08-12T10:00:00", "event": "order_placed", "symbol": "AAPL"},
            {"ts": "2026-08-12T09:00:00", "event": "signal_detected", "symbol": "MSFT"},
        ]
        (tmp_path / "trade_log.jsonl").write_text("\n".join(json.dumps(line) for line in lines))
        df = dashboard.load_trade_log()
        assert list(df["symbol"]) == ["MSFT", "AAPL"]

    def test_skips_malformed_lines(self, tmp_path):
        (tmp_path / "trade_log.jsonl").write_text(
            '{"ts": "2026-08-12T09:00:00", "symbol": "AAPL"}\nnot json\n\n'
        )
        df = dashboard.load_trade_log()
        assert len(df) == 1

    def test_tail_limits_row_count(self, tmp_path):
        lines = [{"ts": f"2026-08-12T{i:02d}:00:00", "symbol": "AAPL"} for i in range(5)]
        (tmp_path / "trade_log.jsonl").write_text("\n".join(json.dumps(line) for line in lines))
        df = dashboard.load_trade_log(tail=2)
        assert len(df) == 2


class TestLoadDailyLogs:
    def test_no_files_returns_empty_dataframe(self):
        df = dashboard.load_daily_logs()
        assert df.empty

    def test_reads_and_sorts_by_date(self, tmp_path):
        (tmp_path / "daily_log_2026-08-11.json").write_text(
            json.dumps({"date": "2026-08-11", "portfolio_value": 99000.0})
        )
        (tmp_path / "daily_log_2026-08-12.json").write_text(
            json.dumps({"date": "2026-08-12", "portfolio_value": 101000.0})
        )
        df = dashboard.load_daily_logs()
        assert list(df["date"]) == ["2026-08-11", "2026-08-12"]

    def test_days_limits_file_count(self, tmp_path):
        for i in range(1, 6):
            (tmp_path / f"daily_log_2026-08-{i:02d}.json").write_text(
                json.dumps({"date": f"2026-08-{i:02d}", "portfolio_value": 100000.0 + i})
            )
        df = dashboard.load_daily_logs(days=2)
        assert list(df["date"]) == ["2026-08-04", "2026-08-05"]

    def test_skips_corrupt_files(self, tmp_path):
        (tmp_path / "daily_log_2026-08-12.json").write_text("not json")
        (tmp_path / "daily_log_2026-08-11.json").write_text(
            json.dumps({"date": "2026-08-11", "portfolio_value": 99000.0})
        )
        df = dashboard.load_daily_logs()
        assert len(df) == 1


class TestLoadAccount:
    def test_broker_failure_degrades_gracefully(self, monkeypatch):
        def _boom():
            raise RuntimeError("Alpaca unreachable")

        monkeypatch.setattr(dashboard, "_get_broker", _boom)
        account, positions, error = dashboard.load_account()
        assert account is None
        assert positions == []
        assert error == "Alpaca unreachable"
