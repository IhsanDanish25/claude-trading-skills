"""BrokerClient is intentionally LIVE-ONLY (paper=False is hardcoded, not
read from config.PAPER_TRADE — see core/broker.py's class docstring for why:
routines/market_open.py etc. always trade the real account through it,
unlike auto_trader.py's own TradingClient(paper=config.PAPER_TRADE)).

That makes ALPACA_PAPER_TRADE/ALPACA_PAPER=true a live/paper mismatch, not a
supported toggle — an operator could watch a paper dashboard while every
order placed through this client executes live (the same class of mismatch
flagged in the OXY/MRK notifier investigation). __init__ must refuse to
start in that case instead of only logging "[LIVE]" and proceeding.
"""

from __future__ import annotations

import pytest

import core.broker as broker_module
from core.broker import BrokerClient


def test_init_raises_when_paper_trade_flag_is_true(monkeypatch):
    monkeypatch.setattr(broker_module, "PAPER_TRADE", True)

    with pytest.raises(RuntimeError, match="LIVE-ONLY"):
        BrokerClient()


def test_init_succeeds_when_paper_trade_flag_is_false(monkeypatch):
    monkeypatch.setattr(broker_module, "PAPER_TRADE", False)
    monkeypatch.setattr(broker_module, "ALPACA_API_KEY", "test-key")
    monkeypatch.setattr(broker_module, "ALPACA_SECRET_KEY", "test-secret")

    # Must not raise -- construction proceeds to build the real Alpaca
    # clients (network-free: TradingClient/StockHistoricalDataClient/
    # CryptoHistoricalDataClient only store credentials at construction).
    broker = BrokerClient()
    assert broker.trade is not None
