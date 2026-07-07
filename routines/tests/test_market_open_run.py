"""Tests for market_open.run() startup sequence."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_open


class TestRunStartup:
    def test_run_records_day_start_before_market_check(self, monkeypatch, tmp_path):
        """Regression: run() must fetch portfolio value before building the
        circuit breaker — referencing pv before assignment killed the whole
        routine with UnboundLocalError at startup."""
        day_start_path = tmp_path / "day_start_value.json"
        broker = MagicMock()
        broker.is_market_open.return_value = False
        broker.portfolio_value.return_value = 100_000.0

        monkeypatch.setattr(market_open.config, "validate", lambda: None)
        monkeypatch.setattr(market_open, "BrokerClient", lambda: broker)
        monkeypatch.setattr(market_open, "DAY_START_PATH", str(day_start_path))
        monkeypatch.setattr(market_open, "_reconcile_closed_trades", lambda b: 0)

        market_open.run()  # market closed → returns early, must not raise

        broker.is_market_open.assert_called_once()
        with open(day_start_path) as f:
            data = json.load(f)
        assert data["value"] == 100_000.0
