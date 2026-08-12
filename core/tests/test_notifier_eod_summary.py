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


def test_eod_summary_renders_blocked_orders_as_explicit_rows_not_garbled_trades(monkeypatch):
    """Regression: blocked orders (spread gate, etc.) used to be fed straight
    into trades_today and rendered by the "Trades Today" formatter, which
    assumes every row has side/qty/price — producing garbled "? sh @ $0.00"
    rows (observed live: MSFT/JPM/GE/C spread-gate blocks, 2026-08-12
    market_open). Blocked attempts must render as their own labeled rows with
    the real symbol and reason, not fabricated trade fields."""
    sent = {}
    monkeypatch.setattr("core.notifier.send", lambda subject, plain, html=None: sent.update(
        subject=subject, plain=plain, html=html) or True)

    ok = send_eod_summary(
        date="2026-08-12",
        portfolio_value=267.0,
        cash=267.0,
        positions_held=0,
        unrealized_pnl=-1.15,
        regime="neutral",
        bias="moderate",
        spy_change_pct=0.0,
        ftd_detected=False,
        blocked_today=[
            {"ts": "2026-08-12T09:40:01", "event": "order_skipped", "symbol": "MSFT",
             "strategy": "earnmom", "gate": "broker_buy", "reason": "spread_too_wide"},
            {"ts": "2026-08-12T09:40:02", "event": "order_skipped", "symbol": "JPM",
             "strategy": "earnmom", "gate": "broker_buy", "reason": "spread_too_wide"},
        ],
    )

    assert ok is True
    assert "?" not in sent["html"].split("Blocked Orders")[1].split("</div>")[0]
    assert "MSFT" in sent["html"] and "spread_too_wide" in sent["html"]
    assert "JPM" in sent["html"]
    assert "$0.00" not in sent["html"]
    assert "Blocked" in sent["plain"] and "MSFT" in sent["plain"]


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
