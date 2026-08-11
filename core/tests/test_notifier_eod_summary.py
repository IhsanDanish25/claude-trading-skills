"""EOD summary now surfaces trades executed today and any routines the
scheduler silently skipped (stale catch-up past the 2h cap) — closing the
blind spot where a bug like the 2026-08-05 flatten could go unnoticed
because nobody was watching logs.
"""

from __future__ import annotations

from core.notifier import send_eod_summary


def test_eod_summary_includes_position_signals(monkeypatch):
    sent = {}

    def fake_send(subject, plain, html=None):
        sent["plain"] = plain
        sent["html"] = html
        return True

    monkeypatch.setattr("core.notifier.send", fake_send)

    ok = send_eod_summary(
        date="2026-08-11",
        portfolio_value=269.21,
        cash=269.21,
        positions_held=1,
        unrealized_pnl=0.0,
        regime="neutral",
        bias="moderate",
        spy_change_pct=0.10,
        ftd_detected=False,
        positions=[
            {"symbol": "NVDA", "action": "HOLD", "reason": "trending above entry, no exit signal"},
            {"symbol": "TSLA", "action": "SELL", "reason": "broke below support"},
            {"symbol": "AAPL", "action": "TIGHTEN_STOP", "reason": "locking in gains"},
        ],
    )

    assert ok is True
    assert "NVDA" in sent["html"] and "badge-hold" in sent["html"]
    assert "TSLA" in sent["html"] and "badge-sell" in sent["html"]
    assert "AAPL" in sent["html"] and "badge-tighten" in sent["html"]
    assert "NVDA=HOLD" in sent["plain"]
    assert "TSLA=SELL" in sent["plain"]


def test_eod_summary_includes_trades_and_skipped_routines(monkeypatch):
    sent = {}

    def fake_send(subject, plain, html=None):
        sent["subject"] = subject
        sent["plain"] = plain
        sent["html"] = html
        return True

    monkeypatch.setattr("core.notifier.send", fake_send)

    ok = send_eod_summary(
        date="2026-08-05",
        portfolio_value=269.59,
        cash=65.51,
        positions_held=1,
        unrealized_pnl=1.15,
        regime="bullish",
        bias="moderate",
        spy_change_pct=0.50,
        ftd_detected=False,
        trades_today=[
            {"ts": "2026-08-05T09:44:33", "symbol": "BRTMU", "side": "buy",
             "qty": 1, "price": 9.99, "strategy": "insider"},
        ],
        skipped_routines=[
            {"module": "routines.midday_review", "reason": "9.0h late, cap=2.0h",
             "at": "18:45:16"},
        ],
    )

    assert ok is True
    assert "BRTMU" in sent["html"]
    assert "insider" in sent["html"]
    assert "midday_review" in sent["html"]
    assert "BRTMU" in sent["plain"]
    assert "SKIPPED" in sent["plain"]


def test_eod_summary_no_trades_no_skips_still_works(monkeypatch):
    monkeypatch.setattr("core.notifier.send", lambda *a, **k: True)

    ok = send_eod_summary(
        date="2026-08-05",
        portfolio_value=269.59,
        cash=65.51,
        positions_held=1,
        unrealized_pnl=1.15,
        regime="bullish",
        bias="moderate",
        spy_change_pct=0.50,
        ftd_detected=False,
    )

    assert ok is True
