"""Tests for spy_base.is_base_symbol()."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import config, spy_base


class TestIsBaseSymbol:
    def test_spy_is_base_when_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "SPY_BASE_ENABLED", True)
        assert spy_base.is_base_symbol("SPY") is True
        assert spy_base.is_base_symbol("spy") is True

    def test_other_symbols_are_not_base(self, monkeypatch):
        monkeypatch.setattr(config, "SPY_BASE_ENABLED", True)
        assert spy_base.is_base_symbol("AAPL") is False

    def test_spy_not_base_when_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "SPY_BASE_ENABLED", False)
        assert spy_base.is_base_symbol("SPY") is False
