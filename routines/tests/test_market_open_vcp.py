"""Tests for the VCP strategy runner (_run_vcp) and its STRATEGY_HANDLERS wiring.

VCP candidates were screened and Claude-scored every morning by pre_market
(state/pre_market_watchlist.json) but never consumed anywhere — market_open's
STRATEGY_HANDLERS never had a "vcp" entry, so the buy_list was inert
regardless of STRATEGY_MODE. These tests cover the added _run_vcp handler:
it reads pre_market's watchlist and, when absent, no-ops instead of raising.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_open


def test_vcp_registered_in_strategy_handlers():
    assert market_open.STRATEGY_HANDLERS["vcp"] is market_open._run_vcp


def test_run_vcp_noop_when_watchlist_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(market_open.config, "STATE_DIR", str(tmp_path))
    slots = [5]

    # Must not raise, must not touch slots, when no pre_market_watchlist.json exists today.
    market_open._run_vcp(
        broker=MagicMock(), cb=MagicMock(), pv=100_000.0, slots=slots,
        held=set(), already_bought_today=set(), sector_counts={},
    )

    assert slots == [5]


def test_run_vcp_buys_scored_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(market_open.config, "STATE_DIR", str(tmp_path))
    watchlist_path = tmp_path / "pre_market_watchlist.json"
    watchlist_path.write_text(json.dumps({
        "buy_list": [{"symbol": "PANW", "score": 78, "reason": "tight VCP near highs"}],
    }))

    monkeypatch.setattr(market_open, "_sector_gate", lambda *a, **k: True)
    monkeypatch.setattr(market_open, "free_cash_for_pead", lambda broker, amount: True)
    monkeypatch.setattr(market_open, "pead_track", lambda *a, **k: None)
    monkeypatch.setattr(market_open, "send_trade_alert", lambda **k: True)
    monkeypatch.setattr(market_open, "_mark_bought", lambda *a, **k: None)
    monkeypatch.setattr(market_open, "_append_trade_log", lambda *a, **k: None)
    monkeypatch.setattr(market_open.trade_logger, "log_event", lambda *a, **k: None)

    broker = MagicMock()
    broker.buy.return_value = {
        "qty": 10, "price": 200.0, "stop": 184.0, "stop_attached": True,
    }
    cb = MagicMock()
    slots = [5]

    market_open._run_vcp(
        broker=broker, cb=cb, pv=100_000.0, slots=slots,
        held=set(), already_bought_today=set(), sector_counts={},
    )

    broker.buy.assert_called_once()
    assert broker.buy.call_args.args[0] == "PANW"
    assert broker.buy.call_args.kwargs["stop_loss_pct"] == market_open.config.VCP_STOP_PCT
    assert slots == [4]


def test_run_vcp_skips_already_held_symbol(monkeypatch, tmp_path):
    monkeypatch.setattr(market_open.config, "STATE_DIR", str(tmp_path))
    watchlist_path = tmp_path / "pre_market_watchlist.json"
    watchlist_path.write_text(json.dumps({
        "buy_list": [{"symbol": "PANW", "score": 78, "reason": "tight VCP"}],
    }))
    monkeypatch.setattr(market_open.trade_logger, "log_event", lambda *a, **k: None)

    broker = MagicMock()
    slots = [5]

    market_open._run_vcp(
        broker=broker, cb=MagicMock(), pv=100_000.0, slots=slots,
        held={"PANW"}, already_bought_today=set(), sector_counts={},
    )

    broker.buy.assert_not_called()
    assert slots == [5]
