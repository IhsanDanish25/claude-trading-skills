"""Unit tests for regime_gate.py's ADX-based trend/chop classifier.

Pure-logic tests. classify() computes ADX/SMA internally from bar history,
so the boundary tests mock _adx to pin an exact reading rather than trying
to engineer synthetic bars that hit a precise ADX value.
"""
from unittest.mock import patch

import regime_gate as rg


def _bars(n=210, drift=0.4, start=400.0):
    """Synthetic uptrending daily closes/highs/lows, oldest -> newest."""
    closes, highs, lows, px = [], [], [], start
    for _ in range(n):
        px *= (1 + drift / 100)
        closes.append(px)
        highs.append(px * 1.005)
        lows.append(px * 0.995)
    return highs, lows, closes


def test_adx_ranging_threshold_lowered_to_18():
    assert rg.ADX_RANGING == 18.0


def test_adx_trending_threshold_unchanged():
    assert rg.ADX_TRENDING == 25.0


def test_todays_1996_adx_no_longer_stands_down():
    """Regression guard for the 2026-08-10 incident: ADX 19.6 in an uptrend
    used to trip STAND_DOWN under the old ADX_RANGING=20.0 threshold. It
    must now clear the gate (NEUTRAL, can_trade=True)."""
    highs, lows, closes = _bars()
    with patch("regime_gate._adx", return_value=19.6):
        regime = rg.classify(highs, lows, closes)
    assert regime.state == "NEUTRAL"
    assert regime.can_trade is True


def test_adx_at_new_floor_still_stands_down():
    """ADX exactly at/just under the new floor is still chop -> STAND_DOWN."""
    highs, lows, closes = _bars()
    with patch("regime_gate._adx", return_value=17.9):
        regime = rg.classify(highs, lows, closes)
    assert regime.state == "STAND_DOWN"
    assert regime.can_trade is False
    assert "ranging chop" in regime.reason


def test_adx_just_above_new_floor_trades():
    highs, lows, closes = _bars()
    with patch("regime_gate._adx", return_value=18.1):
        regime = rg.classify(highs, lows, closes)
    assert regime.state == "NEUTRAL"
    assert regime.can_trade is True


def test_strong_trend_still_go():
    """Untouched GO path: strong uptrend + ADX > 25 -> GO."""
    highs, lows, closes = _bars()
    with patch("regime_gate._adx", return_value=30.0):
        regime = rg.classify(highs, lows, closes)
    assert regime.state == "GO"
    assert regime.can_trade is True


def test_downtrend_still_stands_down_regardless_of_adx():
    """Untouched downtrend path: SMA50 < SMA200 stands down even with a
    strong ADX reading."""
    highs, lows, closes = _bars(drift=-0.4, start=400.0)
    with patch("regime_gate._adx", return_value=30.0):
        regime = rg.classify(highs, lows, closes)
    assert regime.state == "STAND_DOWN"
    assert regime.can_trade is False
