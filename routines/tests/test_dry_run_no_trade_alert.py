"""Regression test for the 2026-09-04 OXY/MRK incident: DRY_RUN=true was set
on Railway, core.broker._dry_run_fill() correctly logged "no order
submitted" and never called Alpaca, but every strategy runner in
market_open.py only checked result["blocked"]/result["stop_attached"]
before emailing — so a simulated fill produced a "BUY" confirmation email
identical to a real one, with nothing distinguishing it as unsent.

_send_trade_alert_if_live() is the fix: it must skip send_trade_alert()
whenever the broker result carries dry_run=True, and behave exactly like a
direct call otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_open


def test_send_trade_alert_if_live_skips_email_on_dry_run(monkeypatch):
    called = []
    monkeypatch.setattr(market_open, "send_trade_alert", lambda **k: called.append(k))

    market_open._send_trade_alert_if_live(
        {"dry_run": True, "qty": 1, "price": 59.55, "stop": 54.78},
        action="BUY", ticker="OXY", shares=1, price=59.55, stop=54.78, target=65.5,
    )

    assert called == []


def test_send_trade_alert_if_live_sends_email_on_real_fill(monkeypatch):
    called = []
    monkeypatch.setattr(market_open, "send_trade_alert", lambda **k: called.append(k))

    market_open._send_trade_alert_if_live(
        {"qty": 1, "price": 59.55, "stop": 54.78, "stop_attached": True},
        action="BUY", ticker="OXY", shares=1, price=59.55, stop=54.78, target=65.5,
    )

    assert len(called) == 1
    assert called[0]["ticker"] == "OXY"


def test_run_earnmom_dry_run_fill_does_not_email(monkeypatch):
    """End-to-end regression for the actual OXY/MRK incident: a DRY_RUN
    buy() result flowing through _run_earnmom must not trigger
    send_trade_alert, even though it reports stop_attached=True (as
    core.broker._dry_run_fill always does) and carries no "blocked" key --
    the two fields every runner checked before this fix."""
    monkeypatch.setattr(market_open, "_sector_gate", lambda *a, **k: True)
    monkeypatch.setattr(market_open, "_timeseries_gate", lambda *a, **k: True)
    monkeypatch.setattr(market_open, "free_cash_for_pead", lambda broker, amount: True)
    monkeypatch.setattr(market_open, "pead_track", lambda *a, **k: None)
    monkeypatch.setattr(market_open, "_mark_bought", lambda *a, **k: None)
    monkeypatch.setattr(market_open, "_append_trade_log", lambda *a, **k: None)
    monkeypatch.setattr(market_open.trade_logger, "log_event", lambda *a, **k: None)

    alerts_sent = []
    monkeypatch.setattr(market_open, "send_trade_alert", lambda **k: alerts_sent.append(k))

    candidates = [{
        "symbol": "OXY", "surprise_pct": 29.0, "age_days": 30,
        "drift_pct": 11.2, "score": 100,
    }]
    monkeypatch.setattr(market_open, "screen_earnmom", lambda: candidates)

    broker = MagicMock()
    broker.buy.return_value = {
        "qty": 1, "price": 59.55, "stop": 54.78, "target": 65.5,
        "stop_attached": True, "target_attached": True, "dry_run": True,
    }
    cb = MagicMock()
    slots = [5]

    market_open._run_earnmom(
        broker=broker, cb=cb, pv=100_000.0, slots=slots,
        held=set(), already_bought_today=set(), sector_counts={},
    )

    broker.buy.assert_called_once()
    assert alerts_sent == [], "DRY_RUN fill must never send a BUY confirmation email"
    # Pipeline validation (the documented purpose of DRY_RUN) still runs:
    assert slots == [4]
