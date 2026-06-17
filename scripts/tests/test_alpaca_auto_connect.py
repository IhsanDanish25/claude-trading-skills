"""Tests for the Alpaca auto-connection chain."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca_auto_connect import (
    _failure_state,
    _snapshot,
    load_connection_state,
    run_chain,
    write_state,
)


# ── fixtures ────────────────────────────────────────────────

FAKE_ACCOUNT = {
    "status": "ACTIVE",
    "account_number": "PA123",
    "equity": "100000.00",
    "cash": "50000.00",
    "buying_power": "200000.00",
    "portfolio_value": "100000.00",
    "account_blocked": False,
    "trading_blocked": False,
}

FAKE_POSITIONS = [
    {
        "symbol": "AAPL",
        "qty": "10",
        "avg_entry_price": "150.00",
        "current_price": "155.00",
        "market_value": "1550.00",
        "unrealized_pl": "50.00",
        "unrealized_plpc": "0.0333",
    }
]

FAKE_ORDERS = [
    {
        "id": "ord_1",
        "symbol": "MSFT",
        "side": "buy",
        "type": "limit",
        "qty": "5",
        "status": "new",
        "created_at": "2026-06-17T10:00:00Z",
    }
]


def _mock_client(account=None, positions=None, orders=None, configured=True):
    client = MagicMock()
    client.is_configured.return_value = configured
    client.api_key = "TESTKEY1234567890"
    client.secret_key = "TESTSECRET"
    client.mode_label = "paper"
    client.paper = True
    client.setup_hint.return_value = "Alpaca credentials not found.\n..."
    client.get_account.return_value = account or FAKE_ACCOUNT
    client.get_positions.return_value = positions if positions is not None else FAKE_POSITIONS
    client.get_orders.return_value = orders if orders is not None else FAKE_ORDERS
    return client


# ── _snapshot ────────────────────────────────────────────────


class TestSnapshot:
    def test_builds_complete_snapshot(self):
        client = _mock_client()
        result = _snapshot(client)

        assert result["connected"] is True
        assert result["mode"] == "paper"
        assert result["account"]["status"] == "ACTIVE"
        assert result["account"]["equity"] == 100000.0
        assert result["positions_count"] == 1
        assert result["positions"][0]["symbol"] == "AAPL"
        assert result["open_orders_count"] == 1
        assert result["open_orders"][0]["symbol"] == "MSFT"
        assert "connected_at" in result

    def test_empty_portfolio(self):
        client = _mock_client(positions=[], orders=[])
        result = _snapshot(client)
        assert result["positions_count"] == 0
        assert result["open_orders_count"] == 0


# ── _failure_state ───────────────────────────────────────────


class TestFailureState:
    def test_structure(self):
        state = _failure_state("credentials_missing", "railway")
        assert state["connected"] is False
        assert state["error"] == "credentials_missing"
        assert state["environment"] == "railway"
        assert state["account"] is None
        assert state["positions"] == []


# ── write / load state ───────────────────────────────────────


class TestStateIO:
    def test_round_trip(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state" / "alpaca_connection.json"
        import alpaca_auto_connect

        monkeypatch.setattr(alpaca_auto_connect, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(alpaca_auto_connect, "STATE_FILE", state_file)

        data = {"connected": True, "mode": "paper"}
        write_state(data)

        loaded = load_connection_state()
        assert loaded["connected"] is True
        assert loaded["mode"] == "paper"

    def test_load_missing_file(self, tmp_path, monkeypatch):
        import alpaca_auto_connect

        monkeypatch.setattr(
            alpaca_auto_connect, "STATE_FILE", tmp_path / "nonexistent.json"
        )
        assert load_connection_state() is None

    def test_load_corrupt_file(self, tmp_path, monkeypatch):
        import alpaca_auto_connect

        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json{{{")
        monkeypatch.setattr(alpaca_auto_connect, "STATE_FILE", bad_file)
        assert load_connection_state() is None


# ── run_chain integration ────────────────────────────────────


class TestRunChain:
    @patch("alpaca_auto_connect.AlpacaClient")
    def test_returns_1_when_not_configured(self, MockClient, tmp_path, monkeypatch):
        import alpaca_auto_connect

        monkeypatch.setattr(alpaca_auto_connect, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(
            alpaca_auto_connect, "STATE_FILE", tmp_path / "state" / "conn.json"
        )

        MockClient.return_value = _mock_client(configured=False)
        result = run_chain(dry_run=False, json_output=False)
        assert result == 1

        state = json.loads((tmp_path / "state" / "conn.json").read_text())
        assert state["connected"] is False
        assert state["error"] == "credentials_missing"

    @patch("alpaca_auto_connect.AlpacaClient")
    def test_returns_0_on_success(self, MockClient, tmp_path, monkeypatch):
        import alpaca_auto_connect

        monkeypatch.setattr(alpaca_auto_connect, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(
            alpaca_auto_connect, "STATE_FILE", tmp_path / "state" / "conn.json"
        )

        MockClient.return_value = _mock_client()
        result = run_chain(dry_run=False, json_output=False)
        assert result == 0

        state = json.loads((tmp_path / "state" / "conn.json").read_text())
        assert state["connected"] is True
        assert state["account"]["equity"] == 100000.0
        assert state["positions_count"] == 1

    @patch("alpaca_auto_connect.AlpacaClient")
    def test_dry_run_does_not_write(self, MockClient, tmp_path, monkeypatch):
        import alpaca_auto_connect

        state_file = tmp_path / "state" / "conn.json"
        monkeypatch.setattr(alpaca_auto_connect, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(alpaca_auto_connect, "STATE_FILE", state_file)

        MockClient.return_value = _mock_client()
        result = run_chain(dry_run=True, json_output=False)
        assert result == 0
        assert not state_file.exists()

    @patch("alpaca_auto_connect.AlpacaClient")
    def test_json_output(self, MockClient, tmp_path, monkeypatch, capsys):
        import alpaca_auto_connect

        monkeypatch.setattr(alpaca_auto_connect, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(
            alpaca_auto_connect, "STATE_FILE", tmp_path / "state" / "conn.json"
        )

        MockClient.return_value = _mock_client()
        result = run_chain(dry_run=False, json_output=True)
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["connected"] is True

    @patch("alpaca_auto_connect.AlpacaClient")
    def test_auth_failure_returns_1(self, MockClient, tmp_path, monkeypatch):
        import alpaca_auto_connect
        from requests import HTTPError

        monkeypatch.setattr(alpaca_auto_connect, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(
            alpaca_auto_connect, "STATE_FILE", tmp_path / "state" / "conn.json"
        )

        client = _mock_client()
        resp = MagicMock()
        resp.status_code = 401
        client.get_account.side_effect = HTTPError(response=resp)
        MockClient.return_value = client

        result = run_chain(dry_run=False, json_output=False)
        assert result == 1

        state = json.loads((tmp_path / "state" / "conn.json").read_text())
        assert state["connected"] is False
        assert "401" in state["error"]

    @patch("alpaca_auto_connect.AlpacaClient")
    def test_network_error_returns_2(self, MockClient, tmp_path, monkeypatch):
        import alpaca_auto_connect
        from requests import ConnectionError as ConnErr

        monkeypatch.setattr(alpaca_auto_connect, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(
            alpaca_auto_connect, "STATE_FILE", tmp_path / "state" / "conn.json"
        )

        client = _mock_client()
        client.get_account.side_effect = ConnErr("timeout")
        MockClient.return_value = client

        result = run_chain(dry_run=False, json_output=False)
        assert result == 2

        state = json.loads((tmp_path / "state" / "conn.json").read_text())
        assert state["error"] == "network_error"
