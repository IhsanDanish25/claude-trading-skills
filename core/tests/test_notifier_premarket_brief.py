"""Pre-market brief now surfaces WATCH-tier VCP candidates alongside BUY ones,
with a BUY/WATCH badge per row, instead of silently dropping anything that
didn't clear the BUY bar in score_vcp_candidates."""

from __future__ import annotations

from core.notifier import send_premarket_brief


def _base_kwargs(**overrides):
    kwargs = dict(
        date="2026-08-11",
        regime="neutral",
        bias="moderate",
        rationale="mixed signals",
        portfolio_value=269.21,
        cash=269.21,
        slots=12,
        buy_list=[],
        high_impact_events=[],
    )
    kwargs.update(overrides)
    return kwargs


def test_premarket_brief_shows_buy_badge(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "core.notifier.send",
        lambda subject, plain, html=None: sent.update(plain=plain, html=html) or True,
    )

    send_premarket_brief(
        **_base_kwargs(
            buy_list=[{"symbol": "AAPL", "score": 82, "reason": "tight base, near 52w high"}]
        )
    )

    assert "AAPL" in sent["html"] and "badge-buy" in sent["html"] and "BUY" in sent["html"]


def test_premarket_brief_shows_watch_list(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "core.notifier.send",
        lambda subject, plain, html=None: sent.update(plain=plain, html=html) or True,
    )

    send_premarket_brief(
        **_base_kwargs(
            watch_list=[{"symbol": "MSFT", "score": 58, "reason": "forming base, not tight yet"}]
        )
    )

    assert "MSFT" in sent["html"] and "badge-watch" in sent["html"] and "WATCH" in sent["html"]
    assert "Watch list: 1" in sent["plain"]


def test_premarket_brief_watch_list_optional(monkeypatch):
    """Backward compat: omitting watch_list entirely must still work."""
    monkeypatch.setattr("core.notifier.send", lambda *a, **k: True)

    ok = send_premarket_brief(**_base_kwargs())

    assert ok is True
