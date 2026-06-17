"""Tests for the routine dispatcher."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run import NEEDS_ALPACA, VALID_ROUTINES, main


class TestDispatcher:
    def test_missing_routine_env_returns_1(self, monkeypatch):
        monkeypatch.delenv("ROUTINE", raising=False)
        assert main() == 1

    def test_invalid_routine_returns_1(self, monkeypatch):
        monkeypatch.setenv("ROUTINE", "nonexistent")
        assert main() == 1

    @patch("run.importlib.import_module")
    @patch("run.run_chain", return_value=0)
    def test_dispatches_pre_market(self, mock_chain, mock_import, monkeypatch):
        monkeypatch.setenv("ROUTINE", "pre_market")
        mock_mod = MagicMock()
        mock_import.return_value = mock_mod

        result = main()
        assert result == 0
        mock_chain.assert_called_once_with(dry_run=False, json_output=False)
        mock_import.assert_called_once_with("pre_market")
        mock_mod.run.assert_called_once()

    @patch("run.importlib.import_module")
    def test_weekly_review_skips_alpaca(self, mock_import, monkeypatch):
        monkeypatch.setenv("ROUTINE", "weekly_review")
        mock_mod = MagicMock()
        mock_import.return_value = mock_mod

        result = main()
        assert result == 0
        mock_import.assert_called_once_with("weekly_review")
        mock_mod.run.assert_called_once()

    @patch("run.run_chain", return_value=1)
    def test_alpaca_failure_aborts_routine(self, mock_chain, monkeypatch):
        monkeypatch.setenv("ROUTINE", "market_open")
        result = main()
        assert result == 1
        mock_chain.assert_called_once()

    @patch("run.importlib.import_module")
    @patch("run.run_chain", return_value=0)
    def test_routine_exception_returns_1(self, mock_chain, mock_import, monkeypatch):
        monkeypatch.setenv("ROUTINE", "midday_review")
        mock_mod = MagicMock()
        mock_mod.run.side_effect = RuntimeError("boom")
        mock_import.return_value = mock_mod

        result = main()
        assert result == 1

    def test_all_valid_routines_have_files(self):
        routines_dir = Path(__file__).resolve().parents[1]
        for name in VALID_ROUTINES:
            assert (routines_dir / f"{name}.py").exists(), f"Missing {name}.py"

    def test_needs_alpaca_is_subset(self):
        assert NEEDS_ALPACA.issubset(VALID_ROUTINES)
