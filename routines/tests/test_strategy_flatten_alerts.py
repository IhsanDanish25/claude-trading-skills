"""Regression tests: every market_open.py strategy runner must alert on a
flatten-on-stop-attach-failure, and must alert again (EMERGENCY) if the
flatten itself fails.

Audit finding while fixing the 2026-08-05 WFC/BAC/NKE flatten-alert
incident: routines/market_open.py's _run_pead already sent a FLATTEN alert
on stop-attach failure (fixed earlier the same day), but eight other
strategy runners (_run_meanrev, _run_insider, _run_squeeze, _run_breakout,
_run_earnmom, _run_gapfill, _run_momentum, _run_sector) flattened the
just-bought position and only wrote an internal trade_logger event — no
send_trade_alert() call at all, so a stop-attach failure in any of these
strategies would force-sell a fresh position with zero notification, same
failure mode as the incident. _run_vcp is covered separately below since
it sources candidates from a watchlist file rather than a screen_*() call.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_open
from core import config


STRATEGIES = [
    ("meanrev", "screen_meanrev",
     {"symbol": "ABC", "price": 10.0, "rsi": 25, "bb_position": 5.0}),
    ("insider", "screen_insider",
     {"symbol": "ABC", "insider_score": 80.0, "n_transactions": 3, "total_dollar": 500_000.0}),
    ("squeeze", "screen_squeeze",
     {"symbol": "ABC", "short_interest_pct": 20.0, "days_to_cover": 5.0,
      "momentum_pct": 3.0, "score": 80}),
    ("breakout", "screen_breakout",
     {"symbol": "ABC", "price": 10.0, "clearance_pct": 2.0, "volume_ratio": 2.0,
      "atr_pct": 3.0, "score": 80}),
    ("earnmom", "screen_earnmom",
     {"symbol": "ABC", "surprise_pct": 10.0, "age_days": 20, "drift_pct": 5.0,
      "score": 80, "report_date": "2026-08-01"}),
    ("gapfill", "screen_gapfill",
     {"symbol": "ABC", "gap_pct": 3.0, "price": 10.0, "prior_close": 9.7, "target": 10.5}),
    ("momentum", "screen_momentum",
     {"symbol": "ABC", "streak_days": 3, "momentum_pct": 5.0, "rel_volume": 2.0,
      "score": 80, "price": 10.0}),
    ("sector", "screen_sector",
     {"symbol": "ABC", "sector": "Tech", "stock_ret": 5.0, "sector_ret": 3.0,
      "rs": 1.5, "score": 80, "price": 10.0}),
]


def _common_patches(monkeypatch):
    monkeypatch.setattr(market_open, "_affordable_candidates", lambda broker, cands, strategy, log: cands)
    monkeypatch.setattr(market_open, "_sector_gate", lambda *a, **k: True)
    monkeypatch.setattr(market_open, "free_cash_for_pead", lambda broker, amount: True)
    monkeypatch.setattr(market_open, "_mark_bought", lambda *a, **k: None)
    monkeypatch.setattr(market_open, "_append_trade_log", lambda *a, **k: None)
    monkeypatch.setattr(market_open, "pead_track", lambda *a, **k: None)
    monkeypatch.setattr(market_open.trade_logger, "log_event", lambda *a, **k: None)


def _broker_with_failed_attach(qty=5, price=10.0):
    broker = MagicMock()
    broker.buy.return_value = {
        "qty": qty, "price": price, "stop": price * 0.9, "target": None,
        "stop_attached": False,
    }
    cb = MagicMock()
    return broker, cb


@pytest.mark.parametrize("strategy,screen_attr,candidate", STRATEGIES)
def test_flatten_on_stop_attach_failure_sends_alert(monkeypatch, strategy, screen_attr, candidate):
    _common_patches(monkeypatch)
    monkeypatch.setattr(market_open, screen_attr, lambda: [dict(candidate)])

    alert = MagicMock()
    monkeypatch.setattr(market_open, "send_trade_alert", alert)

    broker, cb = _broker_with_failed_attach()
    runner = getattr(market_open, f"_run_{strategy}")
    runner(broker=broker, cb=cb, pv=100_000.0, slots=[5],
           held=set(), already_bought_today=set(), sector_counts={})

    broker.sell.assert_called_once_with("ABC", qty=5)
    alert.assert_called_once()
    kwargs = alert.call_args.kwargs
    assert kwargs["action"] == "FLATTEN"
    assert kwargs["ticker"] == "ABC"
    assert kwargs["shares"] == 5


@pytest.mark.parametrize("strategy,screen_attr,candidate", STRATEGIES)
def test_flatten_failure_itself_sends_emergency_alert(monkeypatch, strategy, screen_attr, candidate):
    """If the flatten's own sell() raises (e.g. an Alpaca hiccup), the
    strategy must still notify — via the EMERGENCY fallback — instead of
    silently leaving the position in an unknown protection state."""
    _common_patches(monkeypatch)
    monkeypatch.setattr(market_open, screen_attr, lambda: [dict(candidate)])

    monkeypatch.setattr(market_open, "send_trade_alert", MagicMock())
    emergency = MagicMock()
    monkeypatch.setattr(market_open, "notify_flatten_failed", emergency)

    broker, cb = _broker_with_failed_attach()
    broker.sell.side_effect = Exception("insufficient qty available for order")
    runner = getattr(market_open, f"_run_{strategy}")
    runner(broker=broker, cb=cb, pv=100_000.0, slots=[5],
           held=set(), already_bought_today=set(), sector_counts={})

    emergency.assert_called_once()
    kwargs = emergency.call_args.kwargs
    assert kwargs["ticker"] == "ABC"


class TestVcpFlattenAlert:
    """_run_vcp sources candidates from pre_market_watchlist.json, not a
    screen_*() call, so it needs its own setup."""

    def _watchlist(self, tmp_path):
        path = tmp_path / "pre_market_watchlist.json"
        path.write_text(json.dumps({
            "generated": datetime.datetime.now().isoformat(),
            "buy_list": [{"symbol": "ABC", "score": 80, "reason": "tight VCP"}],
        }))
        return path

    def test_flatten_on_stop_attach_failure_sends_alert(self, monkeypatch, tmp_path):
        _common_patches(monkeypatch)
        monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
        monkeypatch.setattr(market_open.config, "STATE_DIR", str(tmp_path))
        self._watchlist(tmp_path)

        alert = MagicMock()
        monkeypatch.setattr(market_open, "send_trade_alert", alert)

        broker, cb = _broker_with_failed_attach()
        market_open._run_vcp(broker=broker, cb=cb, pv=100_000.0, slots=[5],
                              held=set(), already_bought_today=set(), sector_counts={})

        broker.sell.assert_called_once_with("ABC", qty=5)
        alert.assert_called_once()
        assert alert.call_args.kwargs["action"] == "FLATTEN"

    def test_flatten_failure_itself_sends_emergency_alert(self, monkeypatch, tmp_path):
        _common_patches(monkeypatch)
        monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
        monkeypatch.setattr(market_open.config, "STATE_DIR", str(tmp_path))
        self._watchlist(tmp_path)

        monkeypatch.setattr(market_open, "send_trade_alert", MagicMock())
        emergency = MagicMock()
        monkeypatch.setattr(market_open, "notify_flatten_failed", emergency)

        broker, cb = _broker_with_failed_attach()
        broker.sell.side_effect = Exception("insufficient qty available for order")
        market_open._run_vcp(broker=broker, cb=cb, pv=100_000.0, slots=[5],
                              held=set(), already_bought_today=set(), sector_counts={})

        emergency.assert_called_once()
        assert emergency.call_args.kwargs["ticker"] == "ABC"
