"""Unit tests for core/timeseries_signal.py — the ARIMA-based directional
confirming filter.

This is NOT a standalone strategy: forecast_direction() must never raise
(any data/fit failure degrades to a neutral, zero-confidence result), and
confirms() must only ever block on a confident DISAGREEMENT — neutral and
low-confidence disagreement both pass through, matching the "agrees or is
neutral" contract in the task this filter was built for.
"""

from __future__ import annotations

import math

from core import timeseries_signal


def _bars_from_closes(closes_oldest_first: list[float]) -> list[dict]:
    """Build the newest-first bar shape forecast_direction() expects."""
    return [{"close": c} for c in reversed(closes_oldest_first)]


def _trending(n: int, start: float = 100.0, step_pct: float = 0.01) -> list[float]:
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + step_pct))
    return closes


# ── forecast_direction ───────────────────────────────────────────────────


def test_insufficient_history_returns_neutral():
    bars = _bars_from_closes(_trending(10))  # well under TIMESERIES_MIN_HISTORY_DAYS
    result = timeseries_signal.forecast_direction(bars)
    assert result["direction"] == "neutral"
    assert result["confidence"] == 0.0
    assert result["reason"] == "insufficient_history"


def test_strong_uptrend_forecasts_long():
    bars = _bars_from_closes(_trending(90, step_pct=0.02))
    result = timeseries_signal.forecast_direction(bars)
    assert result["direction"] == "long"
    assert 0.0 <= result["confidence"] <= 1.0
    assert not math.isnan(result["confidence"])


def test_strong_downtrend_forecasts_short():
    bars = _bars_from_closes(_trending(90, step_pct=-0.02))
    result = timeseries_signal.forecast_direction(bars)
    assert result["direction"] == "short"


def test_never_raises_on_garbage_bars():
    # Missing/None closes, empty list, non-numeric-adjacent edge cases.
    for bars in ([], [{"close": None}] * 80, [{}] * 80):
        result = timeseries_signal.forecast_direction(bars)
        assert result["direction"] == "neutral"
        assert result["confidence"] == 0.0


def test_model_error_degrades_to_neutral(monkeypatch):
    # Force the ARIMA import/fit path to blow up; must not propagate.
    bars = _bars_from_closes(_trending(90))

    class _ExplodingARIMA:
        def __init__(self, *a, **k):
            raise RuntimeError("simulated fit failure")

    monkeypatch.setattr("statsmodels.tsa.arima.model.ARIMA", _ExplodingARIMA)
    result = timeseries_signal.forecast_direction(bars)
    assert result["direction"] == "neutral"
    assert result["confidence"] == 0.0
    assert "model_error" in result["reason"]


# ── confirms ──────────────────────────────────────────────────────────────


def test_confirms_allows_when_model_agrees():
    bars = _bars_from_closes(_trending(90, step_pct=0.02))  # forecasts "long"
    result = timeseries_signal.confirms("long", bars, min_confidence=0.0)
    assert result["allowed"] is True


def test_confirms_allows_when_model_neutral(monkeypatch):
    monkeypatch.setattr(
        timeseries_signal,
        "forecast_direction",
        lambda bars: {"direction": "neutral", "confidence": 0.0, "predicted_pct_change": 0.0},
    )
    result = timeseries_signal.confirms("long", bars_newest_first=[], min_confidence=0.5)
    assert result["allowed"] is True


def test_confirms_allows_low_confidence_disagreement(monkeypatch):
    monkeypatch.setattr(
        timeseries_signal,
        "forecast_direction",
        lambda bars: {"direction": "short", "confidence": 0.2, "predicted_pct_change": -0.01},
    )
    result = timeseries_signal.confirms("long", bars_newest_first=[], min_confidence=0.6)
    assert result["allowed"] is True


def test_confirms_blocks_confident_disagreement(monkeypatch):
    monkeypatch.setattr(
        timeseries_signal,
        "forecast_direction",
        lambda bars: {"direction": "short", "confidence": 0.9, "predicted_pct_change": -0.03},
    )
    result = timeseries_signal.confirms("long", bars_newest_first=[], min_confidence=0.6)
    assert result["allowed"] is False
    assert result["direction"] == "short"
