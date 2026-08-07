"""Unit test for EarnMom's per-symbol earnings-fetch failure visibility.

Regression: a per-symbol /stable/earnings fetch failure (402 tier
restriction, network error, etc.) was caught and logged at debug level
only, then silently skipped. From the outside, "every symbol's fetch
failed" and "no one beat earnings today" both just produced "EarnMom: 0
candidates" — a real API problem was indistinguishable from a quiet
market day. screen() now tracks fetch failures and logs a warning-level
summary when any occur.
"""

from __future__ import annotations

from unittest.mock import patch

from core import earnings_momentum_screener as ems


def _patch_common(monkeypatch):
    monkeypatch.setattr(ems, "SP80_UNIVERSE", ["AAPL", "MSFT", "GOOGL"])
    monkeypatch.setattr(ems, "_fetch_bars_batch", lambda symbols: {})
    monkeypatch.setattr(ems, "fmp_remaining_calls", lambda: 1000)


def test_warns_when_some_symbol_fetches_fail(monkeypatch):
    _patch_common(monkeypatch)

    def fake_load(sym):
        if sym == "AAPL":
            raise RuntimeError("402 Payment Required")
        return []

    monkeypatch.setattr(ems, "_load_symbol_earnings", fake_load)

    with patch.object(ems.log, "warning") as mock_warning:
        ems.screen()

    messages = [c.args[0] for c in mock_warning.call_args_list]
    assert any("symbol earnings fetches failed" in m for m in messages)
    # 1 of 3 symbols failed
    warn_call = next(
        c for c in mock_warning.call_args_list if "symbol earnings fetches failed" in c.args[0]
    )
    assert warn_call.args[1:] == (1, 3)


def test_no_warning_when_all_symbol_fetches_succeed(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(ems, "_load_symbol_earnings", lambda sym: [])

    with patch.object(ems.log, "warning") as mock_warning:
        ems.screen()

    messages = [c.args[0] for c in mock_warning.call_args_list]
    assert not any("symbol earnings fetches failed" in m for m in messages)
