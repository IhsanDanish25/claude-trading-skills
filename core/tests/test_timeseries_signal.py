"""Unit tests for core/timeseries_signal.py — the directional confirming
filter, covering both model backends (ARIMA and LSTM).

This is NOT a standalone strategy: forecast_direction() must never raise
(any data/fit/train failure degrades to a neutral, zero-confidence result),
and confirms() must only ever block on a confident DISAGREEMENT — neutral
and low-confidence disagreement both pass through, matching the "agrees or
is neutral" contract this filter was built for.
"""

from __future__ import annotations

import math

import pytest

from core import timeseries_signal


def _bars_from_closes(closes_oldest_first: list[float]) -> list[dict]:
    """Build the newest-first bar shape forecast_direction() expects."""
    return [{"close": c} for c in reversed(closes_oldest_first)]


def _trending(n: int, start: float = 100.0, step_pct: float = 0.01) -> list[float]:
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + step_pct))
    return closes


# ── forecast_direction: shared/dispatch behavior ─────────────────────────


def test_insufficient_history_returns_neutral():
    bars = _bars_from_closes(_trending(10))  # well under TIMESERIES_MIN_HISTORY_DAYS
    result = timeseries_signal.forecast_direction(bars)
    assert result["direction"] == "neutral"
    assert result["confidence"] == 0.0
    assert result["reason"] == "insufficient_history"


def test_unknown_model_returns_neutral():
    bars = _bars_from_closes(_trending(90))
    result = timeseries_signal.forecast_direction(bars, model="not_a_real_model")
    assert result["direction"] == "neutral"
    assert "unknown_model" in result["reason"]


def test_default_model_is_lstm():
    from core.config import TIMESERIES_MODEL

    assert TIMESERIES_MODEL == "lstm"


# ── ARIMA backend ─────────────────────────────────────────────────────────


def test_arima_strong_uptrend_forecasts_long():
    bars = _bars_from_closes(_trending(90, step_pct=0.02))
    result = timeseries_signal.forecast_direction(bars, model="arima")
    assert result["direction"] == "long"
    assert 0.0 <= result["confidence"] <= 1.0
    assert not math.isnan(result["confidence"])


def test_arima_strong_downtrend_forecasts_short():
    bars = _bars_from_closes(_trending(90, step_pct=-0.02))
    result = timeseries_signal.forecast_direction(bars, model="arima")
    assert result["direction"] == "short"


def test_arima_never_raises_on_garbage_bars():
    for bars in ([], [{"close": None}] * 80, [{}] * 80):
        result = timeseries_signal.forecast_direction(bars, model="arima")
        assert result["direction"] == "neutral"
        assert result["confidence"] == 0.0


def test_arima_model_error_degrades_to_neutral(monkeypatch):
    bars = _bars_from_closes(_trending(90))

    class _ExplodingARIMA:
        def __init__(self, *a, **k):
            raise RuntimeError("simulated fit failure")

    monkeypatch.setattr("statsmodels.tsa.arima.model.ARIMA", _ExplodingARIMA)
    result = timeseries_signal.forecast_direction(bars, model="arima")
    assert result["direction"] == "neutral"
    assert result["confidence"] == 0.0
    assert "model_error" in result["reason"]


# ── LSTM backend ──────────────────────────────────────────────────────────

torch = pytest.importorskip("torch")


def test_lstm_never_raises_on_garbage_bars():
    for bars in ([], [{"close": None}] * 80, [{}] * 80):
        result = timeseries_signal.forecast_direction(bars, model="lstm")
        assert result["direction"] == "neutral"
        assert result["confidence"] == 0.0


def test_lstm_insufficient_returns_for_training_stays_neutral():
    # Enough closes to pass the outer TIMESERIES_MIN_HISTORY_DAYS gate, but
    # too few return-sequences to train on at a large lookback.
    closes = _trending(65)
    result = timeseries_signal._forecast_lstm(closes, lookback=60, epochs=5)
    assert result["direction"] == "neutral"
    assert result["reason"] == "insufficient_history_for_lstm"


def test_lstm_learns_pure_uptrend_direction():
    # A perfectly deterministic positive-return sequence is a trivial
    # training signal -- the model should learn "always up" and predict long.
    bars = _bars_from_closes(_trending(90, step_pct=0.01))
    result = timeseries_signal.forecast_direction(bars, model="lstm")
    assert result["direction"] == "long"
    assert 0.0 <= result["confidence"] <= 1.0
    assert not math.isnan(result["confidence"])


def test_lstm_learns_pure_downtrend_direction():
    bars = _bars_from_closes(_trending(90, step_pct=-0.01))
    result = timeseries_signal.forecast_direction(bars, model="lstm")
    assert result["direction"] == "short"


def test_lstm_train_failure_degrades_to_neutral(monkeypatch):
    bars = _bars_from_closes(_trending(90))

    def _raise(*a, **k):
        raise RuntimeError("simulated torch failure")

    monkeypatch.setattr("torch.tensor", _raise)
    result = timeseries_signal.forecast_direction(bars, model="lstm")
    assert result["direction"] == "neutral"
    assert result["confidence"] == 0.0
    assert "model_error" in result["reason"]


# ── confirms (model-agnostic — mocks forecast_direction) ─────────────────


def test_confirms_allows_when_model_agrees(monkeypatch):
    monkeypatch.setattr(
        timeseries_signal,
        "forecast_direction",
        lambda bars, model=None: {
            "direction": "long",
            "confidence": 0.9,
            "predicted_pct_change": 0.01,
        },
    )
    result = timeseries_signal.confirms("long", bars_newest_first=[], min_confidence=0.0)
    assert result["allowed"] is True


def test_confirms_allows_when_model_neutral(monkeypatch):
    monkeypatch.setattr(
        timeseries_signal,
        "forecast_direction",
        lambda bars, model=None: {
            "direction": "neutral",
            "confidence": 0.0,
            "predicted_pct_change": 0.0,
        },
    )
    result = timeseries_signal.confirms("long", bars_newest_first=[], min_confidence=0.5)
    assert result["allowed"] is True


def test_confirms_allows_low_confidence_disagreement(monkeypatch):
    monkeypatch.setattr(
        timeseries_signal,
        "forecast_direction",
        lambda bars, model=None: {
            "direction": "short",
            "confidence": 0.2,
            "predicted_pct_change": -0.01,
        },
    )
    result = timeseries_signal.confirms("long", bars_newest_first=[], min_confidence=0.6)
    assert result["allowed"] is True


def test_confirms_blocks_confident_disagreement(monkeypatch):
    monkeypatch.setattr(
        timeseries_signal,
        "forecast_direction",
        lambda bars, model=None: {
            "direction": "short",
            "confidence": 0.9,
            "predicted_pct_change": -0.03,
        },
    )
    result = timeseries_signal.confirms("long", bars_newest_first=[], min_confidence=0.6)
    assert result["allowed"] is False
    assert result["direction"] == "short"
