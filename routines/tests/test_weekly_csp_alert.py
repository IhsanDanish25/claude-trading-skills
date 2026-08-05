"""Regression tests for weekly_csp.py bugs found while auditing the
2026-08-05 trade-alert incident.

1. send_trade_alert() was called with symbol=/side=/qty=/strategy=/note=
   kwargs that don't exist on its real signature
   (action, ticker, shares, price, stop, target, confidence, reason) —
   a TypeError on every successful CSP execution, caught only by the local
   `except Exception as ne: log.warning(...)` around the call, so the CSP
   still executed with real money but no trade alert was ever sent. Same
   bug class as the flatten-alert incident, different call site.

2. The regime classifier built a dict literal keyed on the boolean
   expressions themselves:
       {spy_chg >= 0.3: "BULLISH", spy_chg >= 0: "NEUTRAL",
        spy_chg >= -0.5: "DEFENSIVE", True: "AVOID_CSP"}
   For any spy_chg, every threshold that holds evaluates to the same key
   (True), so later entries silently overwrote earlier ones — the dict
   always collapsed to {True: "AVOID_CSP"} (plus {False: "DEFENSIVE"} when
   no threshold held). regime was AVOID_CSP on every single run regardless
   of spy_chg, so CSP execution never fired live. Fixed with an if/elif
   chain.

3. screen_csp_candidates(broker, min_premium=10) used a kwarg
   (min_premium) that doesn't exist on the real signature
   (min_premium_pct) — TypeError on every run, uncaught (this call sits
   outside any try/except), so weekly_csp.run() crashed before ever
   screening candidates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import weekly_csp
from core import config


def _common_patches(monkeypatch, tmp_path, spy_chg):
    import core.fmp as fmp_module

    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(weekly_csp.config, "validate", lambda: None)
    # STATE_FILE is built from config.STATE_DIR once at import time, so
    # patching config.STATE_DIR alone doesn't redirect it — patch it
    # directly too.
    monkeypatch.setattr(weekly_csp, "STATE_FILE", str(tmp_path / "weekly_csp_order.json"))
    monkeypatch.setattr(fmp_module, "get_market_breadth", lambda: {"spy_change_pct": spy_chg})


@pytest.mark.parametrize("spy_chg,expected_regime", [
    (0.5, "BULLISH"),
    (0.3, "BULLISH"),
    (0.1, "NEUTRAL"),
    (0.0, "NEUTRAL"),
    (-0.2, "DEFENSIVE"),
    (-0.5, "DEFENSIVE"),
    (-1.0, "AVOID_CSP"),
])
def test_regime_classification_matches_spy_change(monkeypatch, tmp_path, spy_chg, expected_regime):
    """Regression: the old dict-literal regime picker always resolved to
    AVOID_CSP regardless of spy_chg. Every threshold band must now
    classify independently."""
    _common_patches(monkeypatch, tmp_path, spy_chg)
    monkeypatch.setattr(weekly_csp, "screen_csp_candidates", lambda broker, **k: [])
    monkeypatch.setattr(weekly_csp, "pick_best", lambda candidates: None)

    broker = MagicMock()
    broker.get_account.return_value = MagicMock(
        portfolio_value=100_000.0, cash=50_000.0, account_number="12345678",
    )
    monkeypatch.setattr(weekly_csp, "BrokerClient", lambda: broker)

    weekly_csp.run()

    import json
    saved = json.loads((tmp_path / "weekly_csp_order.json").read_text())
    assert saved["regime"] == expected_regime


def test_screen_csp_candidates_called_with_real_signature(monkeypatch, tmp_path):
    """Regression: screen_csp_candidates(broker, min_premium=10) used a
    kwarg name (min_premium) that doesn't exist on the real function
    (min_premium_pct) — call the REAL screener (not mocked) so a
    regression here raises TypeError like it did live."""
    from core import csp_screener, meanrev_screener

    _common_patches(monkeypatch, tmp_path, spy_chg=0.5)  # BULLISH — reaches the screen call
    # Skip the live MeanRev sub-screener (network) — irrelevant to the
    # min_premium_pct kwarg bug this test targets. Falls back to the
    # screener's own fixed cheap-liquid-names watchlist (price=0 stubs).
    monkeypatch.setattr(meanrev_screener, "screen", lambda: [])

    broker = MagicMock()
    broker.get_account.return_value = MagicMock(
        portfolio_value=100_000.0, cash=50_000.0, account_number="12345678",
        options_buying_power=50_000.0,
    )
    broker.get_price.return_value = 50.0
    broker.get_put_contracts.return_value = []
    broker.options_level.return_value = 1
    broker.sell_csp.return_value = {"blocked": True, "reason": "not exercised in this test"}
    monkeypatch.setattr(weekly_csp, "BrokerClient", lambda: broker)
    monkeypatch.setattr(weekly_csp, "screen_csp_candidates", csp_screener.screen_csp_candidates)
    monkeypatch.setattr(weekly_csp, "pick_best", csp_screener.pick_best)

    # Must not raise TypeError for an unexpected keyword argument.
    weekly_csp.run()


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

    _common_patches(monkeypatch, tmp_path, spy_chg=0.1)  # NEUTRAL — reaches execution

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
