"""Regression test for weekly_csp's CSP-execution alert.

weekly_csp.run() called send_trade_alert() with kwargs (symbol, side, qty,
price, strategy, note) that don't exist on the function's real signature
(action, ticker, shares, price, stop, target, confidence, reason). Every
CSP execution therefore raised
`TypeError: send_trade_alert() got an unexpected keyword argument 'symbol'`,
swallowed by the surrounding `except Exception as ne: log.warning(...)`, so
the CSP order executed silently with no trade alert ever going out. Fixed to
use the real kwargs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import weekly_csp

import core.fmp as fmp_module
import core.notifier as notifier_module


def _best_pick():
    return {
        "symbol": "INTC",
        "strike": 30.0,
        "expiration": "2026-08-14",
        "premium_per_share": 0.5,
        "premium": 50.0,
        "premium_pct": 1.5,
        "collateral": 3000.0,
        "dte": 7,
        "rsi": 28,
        "meanrev_score": 80,
    }


def _make_broker(premium_collected=12.5):
    broker = MagicMock()
    acct = MagicMock()
    acct.portfolio_value = 10_000.0
    acct.cash = 5_000.0
    acct.account_number = "TEST123"
    broker.get_account.return_value = acct
    broker.options_level.return_value = 1
    broker.sell_csp.return_value = {"blocked": False, "premium_collected": premium_collected}
    return broker


def _run_csp_execution(monkeypatch, tmp_path):
    """Drive weekly_csp.run() down the BULLISH auto-execute path."""
    monkeypatch.setattr(weekly_csp, "STATE_FILE", str(tmp_path / "weekly_csp_order.json"))
    monkeypatch.setattr(weekly_csp.config, "validate", lambda: None)

    def _raise_breadth_error():
        # Route through the `except` fallback, which sets regime = "NEUTRAL"
        # and reaches the auto-execute path, independent of the regime
        # classifier under test elsewhere in this file.
        raise RuntimeError("breadth unavailable")

    monkeypatch.setattr(fmp_module, "get_market_breadth", _raise_breadth_error)

    best = _best_pick()
    monkeypatch.setattr(weekly_csp, "screen_csp_candidates", lambda broker, min_premium=10: [best])
    monkeypatch.setattr(weekly_csp, "pick_best", lambda candidates: best)

    broker = _make_broker()
    monkeypatch.setattr(weekly_csp, "BrokerClient", lambda: broker)

    return broker


def test_csp_execution_alert_uses_the_real_send_trade_alert_kwargs(monkeypatch, tmp_path):
    broker = _run_csp_execution(monkeypatch, tmp_path)

    alert = MagicMock()
    monkeypatch.setattr(notifier_module, "send_trade_alert", alert)

    weekly_csp.run()

    broker.sell_csp.assert_called_once()
    alert.assert_called_once()
    kwargs = alert.call_args.kwargs
    # The bug: these were previously named symbol/side/qty/strategy/note,
    # none of which match send_trade_alert()'s real signature.
    assert kwargs["action"] == "SELL_TO_OPEN"
    assert kwargs["ticker"] == "INTC"
    assert kwargs["shares"] == 1
    assert kwargs["price"] == 30.0
    assert "stop" in kwargs
    assert "target" in kwargs
    assert "premium=$12.50" in kwargs["reason"]


def test_regime_classifier_selects_the_correct_bucket_not_always_avoid_csp(monkeypatch, tmp_path):
    """weekly_csp's regime classifier previously built its buckets as a
    boolean-keyed dict: {spy_chg >= 0.3: "BULLISH", spy_chg >= 0: "NEUTRAL",
    spy_chg >= -0.5: "DEFENSIVE", True: "AVOID_CSP"}. Every one of those keys
    is either True or False, dict literals collapse duplicate keys to the
    last value written for that key, and the final entry's key is always
    literal True — so whenever spy_chg cleared any threshold, the dict
    collapsed to a single {True: "AVOID_CSP"} entry regardless of the actual
    SPY change, permanently blocking CSP auto-execution. This drives the real
    branch (not the exception fallback) for the full threshold range."""
    monkeypatch.setattr(weekly_csp, "STATE_FILE", str(tmp_path / "weekly_csp_order.json"))
    monkeypatch.setattr(weekly_csp.config, "validate", lambda: None)
    monkeypatch.setattr(weekly_csp, "screen_csp_candidates", lambda broker, min_premium=10: [])
    monkeypatch.setattr(weekly_csp, "pick_best", lambda candidates: None)
    monkeypatch.setattr(weekly_csp, "BrokerClient", lambda: _make_broker())

    cases = [
        (1.0, "BULLISH"),
        (0.3, "BULLISH"),
        (0.1, "NEUTRAL"),
        (0.0, "NEUTRAL"),
        (-0.2, "DEFENSIVE"),
        (-0.5, "DEFENSIVE"),
        (-1.0, "AVOID_CSP"),
    ]
    for spy_chg, expected_regime in cases:
        monkeypatch.setattr(
            fmp_module,
            "get_market_breadth",
            lambda spy_chg=spy_chg: {"spy_change_pct": spy_chg},
        )
        weekly_csp.run()
        with open(weekly_csp.STATE_FILE) as f:
            saved = json.load(f)
        assert saved["regime"] == expected_regime, (
            f"spy_chg={spy_chg}: expected {expected_regime}, got {saved['regime']}"
        )


def test_csp_execution_alert_is_never_swallowed_by_a_real_signature_mismatch(monkeypatch, tmp_path):
    """Use the real send_trade_alert (not a mock) so a kwarg mismatch would
    raise TypeError exactly like the incident, and would get silently
    swallowed by weekly_csp's `except Exception as ne: log.warning(...)`.
    No network mocking needed: send_trade_alert() silently no-ops when
    RESEND_API_KEY isn't set (core/notifier.py:send), so this stays a pure
    signature/argument-completeness test."""
    monkeypatch.setattr(notifier_module, "_API_KEY", "")

    _run_csp_execution(monkeypatch, tmp_path)

    warnings = []
    monkeypatch.setattr(
        weekly_csp.log,
        "warning",
        lambda msg, *args: warnings.append(msg % args if args else msg),
    )

    weekly_csp.run()

    assert not any("Notify failed" in w for w in warnings), (
        f"send_trade_alert() raised and was swallowed: {warnings}"
    )
