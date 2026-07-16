"""Regression test for worker.startup_health_check() Alpaca mode.

core.broker.BrokerClient always connects live now (see core/broker.py), but
the health check builds its own AlpacaClient separately. Before this fix it
defaulted to paper mode, so with live-only keys it correctly traded through
BrokerClient yet the health check still reported a false 401 against
paper-api.alpaca.markets on every boot.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker


class TestStartupHealthCheckAlpacaMode:
    def test_alpaca_client_constructed_with_paper_false(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "livekey")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "livesecret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropickey")
        monkeypatch.setenv("FMP_API_KEY", "fmpkey")

        fake_client = MagicMock()
        fake_client.get_account.return_value = {
            "equity": "70.63",
            "cash": "70.63",
            "status": "ACTIVE",
        }

        with patch("alpaca_client.AlpacaClient", return_value=fake_client) as mock_cls:
            worker.startup_health_check()

        mock_cls.assert_called_once_with(paper=False)
