"""Tests for the trading dashboard app."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


def _mock_account(**overrides) -> dict:
    base = {
        "id": "test-123",
        "status": "ACTIVE",
        "equity": "105000.00",
        "cash": "50000.00",
        "buying_power": "100000.00",
        "portfolio_value": "105000.00",
        "last_equity": "100000.00",
        "daytrade_count": 1,
        "pattern_day_trader": False,
    }
    base.update(overrides)
    return base


def _mock_position(symbol="AAPL", qty="10", avg="150.00", current="155.00") -> dict:
    unrealized = (float(current) - float(avg)) * float(qty)
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": avg,
        "current_price": current,
        "market_value": str(float(current) * float(qty)),
        "unrealized_pl": str(unrealized),
        "unrealized_plpc": str(unrealized / (float(avg) * float(qty))),
    }


def _mock_order(symbol="AAPL", side="buy", order_type="limit") -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "qty": "10",
        "limit_price": "150.00",
        "stop_price": None,
        "status": "new",
        "submitted_at": "2026-06-17T10:00:00Z",
    }


class TestLoadFunctions:
    @patch("app.AlpacaClient")
    def test_load_account_success(self, mock_cls):
        from app import load_account
        client = MagicMock()
        client.get_account.return_value = _mock_account()
        result = load_account(client)
        assert result is not None
        assert result["equity"] == "105000.00"

    @patch("app.AlpacaClient")
    def test_load_account_failure(self, mock_cls):
        from app import load_account
        client = MagicMock()
        client.get_account.side_effect = Exception("Network error")
        # load_account calls st.error which needs streamlit context
        # Just verify it handles the exception
        with patch("app.st"):
            result = load_account(client)
        assert result is None

    @patch("app.AlpacaClient")
    def test_load_positions_success(self, mock_cls):
        from app import load_positions
        client = MagicMock()
        client.get_positions.return_value = [_mock_position(), _mock_position("MSFT")]
        result = load_positions(client)
        assert len(result) == 2

    @patch("app.AlpacaClient")
    def test_load_positions_failure(self, mock_cls):
        from app import load_positions
        client = MagicMock()
        client.get_positions.side_effect = Exception("fail")
        result = load_positions(client)
        assert result == []

    @patch("app.AlpacaClient")
    def test_load_orders_success(self, mock_cls):
        from app import load_orders
        client = MagicMock()
        client.get_orders.return_value = [_mock_order()]
        result = load_orders(client)
        assert len(result) == 1

    @patch("app.AlpacaClient")
    def test_load_orders_failure(self, mock_cls):
        from app import load_orders
        client = MagicMock()
        client.get_orders.side_effect = Exception("fail")
        result = load_orders(client)
        assert result == []


class TestTVIndicators:
    def test_load_tv_indicators_success(self):
        from app import load_tv_indicators
        mock_mod = MagicMock()
        mock_mod.fetch_multi.return_value = [
            {"symbol": "AAPL", "summary": {"RECOMMENDATION": "BUY"}}
        ]
        with patch.dict(sys.modules, {"tv_scanner": mock_mod}):
            result = load_tv_indicators(["AAPL"])
        assert len(result) == 1

    def test_load_tv_indicators_import_fail(self):
        from app import load_tv_indicators
        with patch.dict(sys.modules, {"tv_scanner": None}):
            result = load_tv_indicators(["AAPL"])
            assert result == []


class TestConstants:
    def test_routine_schedule_has_5_entries(self):
        from app import ROUTINE_SCHEDULE
        assert len(ROUTINE_SCHEDULE) == 5

    def test_routine_schedule_structure(self):
        from app import ROUTINE_SCHEDULE
        for name, routine_id, time, days in ROUTINE_SCHEDULE:
            assert isinstance(name, str)
            assert isinstance(routine_id, str)
            assert "UTC" in time
            assert isinstance(days, str)
