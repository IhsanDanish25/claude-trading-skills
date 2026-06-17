"""Tests for the centralized Alpaca client module."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is importable
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca_client import (
    LIVE_BASE_URL,
    PAPER_BASE_URL,
    AlpacaClient,
    is_railway,
)


# ── is_railway detection ────────────────────────────────────


class TestIsRailway:
    def test_false_by_default(self, monkeypatch):
        monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
        monkeypatch.delenv("RAILWAY_SERVICE_NAME", raising=False)
        monkeypatch.delenv("RAILWAY_PROJECT_NAME", raising=False)
        assert is_railway() is False

    @pytest.mark.parametrize(
        "var",
        ["RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME", "RAILWAY_PROJECT_NAME"],
    )
    def test_true_when_var_set(self, monkeypatch, var):
        monkeypatch.setenv(var, "production")
        assert is_railway() is True


# ── credential loading ──────────────────────────────────────


class TestCredentialLoading:
    def test_explicit_args(self):
        c = AlpacaClient(api_key="key1", secret_key="sec1", paper=True)
        assert c.api_key == "key1"
        assert c.secret_key == "sec1"
        assert c.paper is True
        assert c.is_configured() is True

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "envkey")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "envsec")
        monkeypatch.setenv("ALPACA_PAPER", "false")
        c = AlpacaClient()
        assert c.api_key == "envkey"
        assert c.secret_key == "envsec"
        assert c.paper is False

    def test_paper_defaults_true(self, monkeypatch):
        monkeypatch.delenv("ALPACA_PAPER", raising=False)
        c = AlpacaClient(api_key="k", secret_key="s")
        assert c.paper is True

    def test_not_configured_when_missing(self, monkeypatch):
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        c = AlpacaClient()
        assert c.is_configured() is False

    def test_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "env")
        c = AlpacaClient(api_key="explicit", secret_key="s")
        assert c.api_key == "explicit"


# ── URL routing ─────────────────────────────────────────────


class TestURLRouting:
    def test_paper_url(self):
        c = AlpacaClient(api_key="k", secret_key="s", paper=True)
        assert c.trading_url == PAPER_BASE_URL

    def test_live_url(self):
        c = AlpacaClient(api_key="k", secret_key="s", paper=False)
        assert c.trading_url == LIVE_BASE_URL

    def test_mode_label(self):
        assert AlpacaClient(api_key="k", secret_key="s", paper=True).mode_label == "paper"
        assert AlpacaClient(api_key="k", secret_key="s", paper=False).mode_label == "live"


# ── setup_hint ──────────────────────────────────────────────


class TestSetupHint:
    def test_local_hint(self, monkeypatch):
        monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
        monkeypatch.delenv("RAILWAY_SERVICE_NAME", raising=False)
        monkeypatch.delenv("RAILWAY_PROJECT_NAME", raising=False)
        c = AlpacaClient()
        hint = c.setup_hint()
        assert "export" in hint
        assert "ALPACA_API_KEY" in hint

    def test_railway_hint(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        c = AlpacaClient()
        hint = c.setup_hint()
        assert "Railway" in hint
        assert "Variables tab" in hint


# ── API methods (mocked) ────────────────────────────────────


def _mock_response(status=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data if json_data is not None else {}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        from requests import HTTPError

        resp.raise_for_status.side_effect = HTTPError(response=resp)
    return resp


class TestAPIMethods:
    def _client(self):
        return AlpacaClient(api_key="testkey", secret_key="testsec", paper=True)

    @patch("alpaca_client.requests.get")
    def test_get_account(self, mock_get):
        mock_get.return_value = _mock_response(
            json_data={"status": "ACTIVE", "equity": "100000"}
        )
        result = self._client().get_account()
        assert result["status"] == "ACTIVE"
        mock_get.assert_called_once()
        assert "/v2/account" in mock_get.call_args[0][0]

    @patch("alpaca_client.requests.get")
    def test_get_positions_empty(self, mock_get):
        mock_get.return_value = _mock_response(json_data=[])
        result = self._client().get_positions()
        assert result == []

    @patch("alpaca_client.requests.get")
    def test_get_positions_with_holdings(self, mock_get):
        mock_get.return_value = _mock_response(
            json_data=[{"symbol": "AAPL", "qty": "10"}]
        )
        result = self._client().get_positions()
        assert len(result) == 1
        assert result[0]["symbol"] == "AAPL"

    @patch("alpaca_client.requests.get")
    def test_get_orders(self, mock_get):
        mock_get.return_value = _mock_response(json_data=[])
        result = self._client().get_orders(status="open", limit=10)
        assert result == []
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"]["status"] == "open"
        assert call_kwargs[1]["params"]["limit"] == 10

    @patch("alpaca_client.requests.get")
    def test_get_portfolio_history(self, mock_get):
        mock_get.return_value = _mock_response(
            json_data={"equity": [100000, 100500], "timestamp": [1, 2]}
        )
        result = self._client().get_portfolio_history(period="1W", timeframe="1D")
        assert "equity" in result
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"]["period"] == "1W"

    @patch("alpaca_client.requests.get")
    def test_get_latest_quote(self, mock_get):
        mock_get.return_value = _mock_response(
            json_data={"quote": {"bp": 150.0, "ap": 150.05}}
        )
        result = self._client().get_latest_quote("AAPL")
        assert result["quote"]["bp"] == 150.0

    @patch("alpaca_client.requests.get")
    def test_get_asset_found(self, mock_get):
        mock_get.return_value = _mock_response(
            json_data={"symbol": "AAPL", "shortable": True}
        )
        result = self._client().get_asset("AAPL")
        assert result["symbol"] == "AAPL"

    @patch("alpaca_client.requests.get")
    def test_get_asset_not_found(self, mock_get):
        resp = MagicMock()
        resp.status_code = 404
        resp.json.return_value = {}
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        result = self._client().get_asset("FAKE")
        assert result is None


# ── verify_connection ────────────────────────────────────────


class TestVerifyConnection:
    def test_raises_when_not_configured(self):
        c = AlpacaClient()
        with pytest.raises(RuntimeError, match="credentials not found"):
            c.verify_connection()

    @patch("alpaca_client.requests.get")
    def test_returns_account_on_success(self, mock_get):
        mock_get.return_value = _mock_response(
            json_data={"status": "ACTIVE"}
        )
        c = AlpacaClient(api_key="k", secret_key="s")
        result = c.verify_connection()
        assert result["status"] == "ACTIVE"

    @patch("alpaca_client.requests.get")
    def test_raises_on_auth_failure(self, mock_get):
        mock_get.return_value = _mock_response(status=401)
        c = AlpacaClient(api_key="bad", secret_key="bad")
        from requests import HTTPError

        with pytest.raises(HTTPError):
            c.verify_connection()


# ── CLI main ─────────────────────────────────────────────────


class TestCLI:
    @patch("alpaca_client.AlpacaClient.is_configured", return_value=False)
    def test_exits_1_when_not_configured(self, _mock):
        from alpaca_client import main

        assert main() == 1

    @patch("alpaca_client.AlpacaClient.get_latest_quote")
    @patch("alpaca_client.AlpacaClient.get_positions", return_value=[])
    @patch(
        "alpaca_client.AlpacaClient.verify_connection",
        return_value={
            "status": "ACTIVE",
            "account_number": "123",
            "equity": "100000",
            "cash": "50000",
            "buying_power": "200000",
            "portfolio_value": "100000",
        },
    )
    @patch("alpaca_client.AlpacaClient.is_configured", return_value=True)
    def test_exits_0_on_success(self, _cfg, _verify, _pos, _quote, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "testkey123")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "testsec")
        _quote.return_value = {"quote": {"bp": 150.0, "ap": 150.05}}
        from alpaca_client import main

        assert main() == 0
