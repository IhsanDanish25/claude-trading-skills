"""Tests for worker.startup_rebalance()."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import worker


class TestStartupRebalance:
    def test_skips_when_env_unset(self, monkeypatch, caplog):
        monkeypatch.delenv("REBALANCE_ON_BOOT", raising=False)
        with caplog.at_level("INFO", logger="worker"):
            worker.startup_rebalance()
        assert "skipping startup rebalance" in caplog.text

    def test_dry_run_calls_build_plan_with_valid_signature(self, monkeypatch, caplog):
        """Regression: worker called build_plan() without keep_symbols and
        crashed with TypeError on every container start."""
        monkeypatch.setenv("REBALANCE_ON_BOOT", "dry")
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.portfolio_value.return_value = 100_000.0
        broker.cash.return_value = 1_000.0

        with patch("core.broker.BrokerClient", return_value=broker):
            with caplog.at_level("INFO", logger="worker"):
                worker.startup_rebalance()

        assert "Startup rebalance failed" not in caplog.text
        assert "No positions — nothing to rebalance" in caplog.text
