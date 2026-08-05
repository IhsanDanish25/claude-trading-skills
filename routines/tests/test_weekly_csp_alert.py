"""Regression test for weekly_csp.py's post-execution alert call.

send_trade_alert() was called with symbol=/side=/qty=/strategy=/note=
kwargs that don't exist on its real signature
(action, ticker, shares, price, stop, target, confidence, reason) —
a TypeError on every successful CSP execution, caught only by the local
`except Exception as ne: log.warning(...)` around the call, so the CSP
still executed with real money but no trade alert was ever sent. Same bug
class as the 2026-08-05 flatten-alert incident, different call site.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import weekly_csp
from core import config


def test_csp_execution_alert_uses_real_send_trade_alert_signature(monkeypatch, tmp_path):
    """Run the real send_trade_alert (not mocked) so a wrong-kwarg
    regression raises TypeError. The call sits inside its own local
    try/except in weekly_csp.py, so the TypeError alone wouldn't fail this
    test — assert the underlying notifier.send() actually gets reached
    instead, since a TypeError on the send_trade_alert() call itself
    would prevent send() from ever being invoked."""
    import core.notifier as notifier_module
    sent = MagicMock(return_value=True)
    monkeypatch.setattr(notifier_module, "send", sent)

    import core.fmp as fmp_module

    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(weekly_csp.config, "validate", lambda: None)

    def _raise_breadth():
        raise RuntimeError("breadth unavailable in test")

    # weekly_csp's own regime dict-literal collapses every key to a single
    # `True` entry (all branches are boolean expressions evaluated eagerly,
    # so duplicate `True` keys overwrite each other) and always resolves to
    # AVOID_CSP regardless of spy_chg — a separate, unrelated bug out of
    # scope here. Route around it via the except-fallback ("assuming
    # NEUTRAL") so this test can reach and verify the alert call.
    monkeypatch.setattr(fmp_module, "get_market_breadth", _raise_breadth)

    candidate = {
        "symbol": "KO", "strike": 60.0, "expiration": "2026-08-14",
        "premium": 0.35, "premium_pct": 0.6, "premium_per_share": 0.35,
        "collateral": 6000.0, "dte": 7, "rsi": 42, "meanrev_score": 80,
        "type": "csp",
    }
    monkeypatch.setattr(weekly_csp, "screen_csp_candidates", lambda broker, **k: [candidate])
    monkeypatch.setattr(weekly_csp, "pick_best", lambda candidates: candidate)

    broker = MagicMock()
    broker.get_account.return_value = MagicMock(
        portfolio_value=100_000.0, cash=50_000.0, account_number="12345678",
    )
    broker.options_level.return_value = 1
    broker.sell_csp.return_value = {
        "order": MagicMock(), "contract": "KO260814P00060000", "symbol": "KO",
        "strike": 60.0, "expiration": "2026-08-14", "qty": 1,
        "fill_price": 0.35, "premium_collected": 35.0, "collateral": 6000.0,
        "status": "filled",
    }
    monkeypatch.setattr(weekly_csp, "BrokerClient", lambda: broker)

    # Must not raise, the CSP must actually execute, and the alert must
    # actually reach notifier.send() — not die inside send_trade_alert()
    # on a wrong-kwarg TypeError before ever getting there.
    weekly_csp.run()
    broker.sell_csp.assert_called_once()
    sent.assert_called_once()
