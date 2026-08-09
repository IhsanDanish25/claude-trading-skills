"""Tests for _timeseries_gate() — the confirming filter that only blocks a
strategy's entry when core.timeseries_signal confidently disagrees.

Key contract under test: TIMESERIES_ENABLED=False (the default) must be a
complete no-op (no bar fetch, no model call), and any fetch/model failure
must default to ALLOW — this is a secondary confirming layer on top of an
already-validated strategy, not a safety check on the order itself, so it
must never introduce a new way for a validated strategy to get blocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_open


def _make_log():
    return MagicMock()


def test_disabled_is_a_complete_noop(monkeypatch):
    monkeypatch.setattr(market_open.config, "TIMESERIES_ENABLED", False)
    fetch_mock = MagicMock(side_effect=AssertionError("should not fetch when disabled"))
    monkeypatch.setattr(market_open, "fetch_bars", fetch_mock)

    result = market_open._timeseries_gate("AAPL", "breakout", _make_log())

    assert result is True
    fetch_mock.assert_not_called()


def test_allows_when_model_confirms(monkeypatch):
    monkeypatch.setattr(market_open.config, "TIMESERIES_ENABLED", True)
    monkeypatch.setattr(market_open.config, "TIMESERIES_MIN_HISTORY_DAYS", 60)
    monkeypatch.setattr(market_open.config, "TIMESERIES_MIN_CONFIDENCE", 0.6)
    monkeypatch.setattr(
        market_open, "fetch_bars", lambda symbols, days: {symbols[0]: [{"close": 100.0}]}
    )
    monkeypatch.setattr(
        market_open.timeseries_signal,
        "confirms",
        lambda direction, bars, min_confidence: {
            "allowed": True,
            "direction": "long",
            "confidence": 0.7,
        },
    )
    log = _make_log()

    result = market_open._timeseries_gate("AAPL", "breakout", log)

    assert result is True


def test_blocks_and_logs_on_confident_disagreement(monkeypatch):
    monkeypatch.setattr(market_open.config, "TIMESERIES_ENABLED", True)
    monkeypatch.setattr(market_open.config, "TIMESERIES_MIN_HISTORY_DAYS", 60)
    monkeypatch.setattr(market_open.config, "TIMESERIES_MIN_CONFIDENCE", 0.6)
    monkeypatch.setattr(
        market_open, "fetch_bars", lambda symbols, days: {symbols[0]: [{"close": 100.0}]}
    )
    monkeypatch.setattr(
        market_open.timeseries_signal,
        "confirms",
        lambda direction, bars, min_confidence: {
            "allowed": False,
            "direction": "short",
            "confidence": 0.85,
        },
    )
    logged = {}
    monkeypatch.setattr(
        market_open.trade_logger,
        "log_event",
        lambda event_type, strategy, symbol=None, **fields: logged.update(
            {"event_type": event_type, "strategy": strategy, "symbol": symbol, **fields}
        ),
    )

    result = market_open._timeseries_gate("AAPL", "breakout", _make_log())

    assert result is False
    assert logged["event_type"] == "gate_failed"
    assert logged["gate"] == "timeseries_confirm"
    assert logged["direction"] == "short"


def test_fetch_failure_defaults_to_allow(monkeypatch):
    monkeypatch.setattr(market_open.config, "TIMESERIES_ENABLED", True)
    monkeypatch.setattr(market_open.config, "TIMESERIES_MIN_HISTORY_DAYS", 60)
    monkeypatch.setattr(market_open.config, "TIMESERIES_MIN_CONFIDENCE", 0.6)

    def _raise(symbols, days):
        raise RuntimeError("Alpaca API timeout")

    monkeypatch.setattr(market_open, "fetch_bars", _raise)

    result = market_open._timeseries_gate("AAPL", "breakout", _make_log())

    assert result is True


def test_model_failure_defaults_to_allow(monkeypatch):
    monkeypatch.setattr(market_open.config, "TIMESERIES_ENABLED", True)
    monkeypatch.setattr(market_open.config, "TIMESERIES_MIN_HISTORY_DAYS", 60)
    monkeypatch.setattr(market_open.config, "TIMESERIES_MIN_CONFIDENCE", 0.6)
    monkeypatch.setattr(
        market_open, "fetch_bars", lambda symbols, days: {symbols[0]: [{"close": 100.0}]}
    )

    def _raise(*a, **k):
        raise RuntimeError("model blew up")

    monkeypatch.setattr(market_open.timeseries_signal, "confirms", _raise)

    result = market_open._timeseries_gate("AAPL", "breakout", _make_log())

    assert result is True
