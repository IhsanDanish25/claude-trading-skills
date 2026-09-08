"""Unit tests for core/macross_screener.py's pure-logic helpers — the
golden-cross detector and its supporting indicators.

These are the parts worth testing in isolation: screen() itself is a thin
wrapper around a yfinance batch fetch (mirrors meanrev/breakout, which are
likewise untested at that level — see backtest_harness/satellite_signals.py
for the point-in-time replica used to actually validate the strategy).
"""

from __future__ import annotations

from core.macross_screener import _avg_volume, _find_recent_cross, _sma, _sma_series

# ── _sma ──────────────────────────────────────────────────────────────────


def test_sma_basic_average():
    assert _sma([1, 2, 3, 4, 5], 5) == 3.0


def test_sma_uses_trailing_window_only():
    # window=3 over the last 3 of 5 values: (3+4+5)/3
    assert _sma([1, 2, 3, 4, 5], 3) == 4.0


def test_sma_insufficient_history_returns_none():
    assert _sma([1, 2], 5) is None


# ── _sma_series ───────────────────────────────────────────────────────────


def test_sma_series_length_matches_input():
    closes = [float(i) for i in range(1, 11)]
    series = _sma_series(closes, 3)
    assert len(series) == len(closes)


def test_sma_series_none_until_window_filled():
    closes = [10.0, 11.0, 12.0]
    series = _sma_series(closes, 3)
    assert series[0] is None
    assert series[1] is None
    assert series[2] == 11.0  # (10+11+12)/3


# ── _avg_volume ───────────────────────────────────────────────────────────


def test_avg_volume_trailing_window():
    vols = [100.0] * 15 + [200.0] * 5  # last 20 values
    assert _avg_volume(vols, n=20) == (100.0 * 15 + 200.0 * 5) / 20


def test_avg_volume_ignores_zero_and_falsy_entries():
    vols = [0.0, 100.0, 0.0, 200.0]
    assert _avg_volume(vols, n=20) == 150.0


def test_avg_volume_empty_returns_zero():
    assert _avg_volume([], n=20) == 0.0


# ── _find_recent_cross ───────────────────────────────────────────────────


def test_finds_cross_on_most_recent_bar():
    # fast crosses above slow exactly at the last index.
    fast = [10.0, 10.0, 10.0, 12.0]
    slow = [11.0, 11.0, 11.0, 11.0]
    idx = _find_recent_cross(fast, slow, max_days_back=3)
    assert idx == 3


def test_finds_cross_within_lookback_window():
    # cross happened 2 bars back (index 2), then fast stays above slow.
    fast = [10.0, 10.0, 12.0, 12.5, 13.0]
    slow = [11.0, 11.0, 11.0, 11.0, 11.0]
    idx = _find_recent_cross(fast, slow, max_days_back=3)
    assert idx == 2


def test_no_cross_returns_none():
    # fast never crosses above slow.
    fast = [9.0, 9.0, 9.0, 9.0]
    slow = [11.0, 11.0, 11.0, 11.0]
    assert _find_recent_cross(fast, slow, max_days_back=3) is None


def test_cross_outside_lookback_window_is_not_found():
    # cross happened 5 bars back — outside a 3-day lookback.
    fast = [12.0, 12.0, 12.0, 12.0, 12.0, 12.0]
    slow = [11.0, 11.0, 11.0, 11.0, 11.0, 11.0]
    fast_with_cross_far_back = [9.0] + fast  # cross was at index 1 of 7 total bars
    slow_matched = [11.0] + slow
    idx = _find_recent_cross(fast_with_cross_far_back, slow_matched, max_days_back=3)
    assert idx is None


def test_already_crossed_before_series_start_is_not_a_cross():
    # fast is already above slow for the whole visible window — no transition observed.
    fast = [12.0, 12.5, 13.0, 13.5]
    slow = [11.0, 11.0, 11.0, 11.0]
    assert _find_recent_cross(fast, slow, max_days_back=3) is None


def test_none_values_are_skipped_without_raising():
    fast = [None, None, 10.0, 12.0]
    slow = [None, None, 11.0, 11.0]
    idx = _find_recent_cross(fast, slow, max_days_back=3)
    assert idx == 3
