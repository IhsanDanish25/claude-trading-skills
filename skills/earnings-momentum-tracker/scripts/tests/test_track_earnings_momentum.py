"""Tests for track_earnings_momentum.py"""

import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from track_earnings_momentum import calc_momentum, get_latest_earnings, grade_momentum

# ---------------------------------------------------------------------------
# calc_momentum / grade_momentum -- pure functions, unchanged by the FMP ->
# yfinance switch, still exercised through the new price-history shape.
# ---------------------------------------------------------------------------


class TestCalcMomentum:
    def test_returns_none_when_window_extends_past_available_data(self):
        prices = [{"date": "2026-01-01", "close": 100}, {"date": "2026-01-02", "close": 101}]
        assert calc_momentum(prices, earnings_date="2026-01-01", window=20) is None

    def test_computes_pct_change_over_window(self):
        prices = [{"date": f"2026-01-{d:02d}", "close": 100 + d} for d in range(1, 15)]
        result = calc_momentum(prices, earnings_date="2026-01-01", window=5)
        assert result == round((106 - 101) / 101 * 100, 2)


class TestGradeMomentum:
    def test_none_input_returns_unknown(self):
        assert grade_momentum(None) == "?"

    def test_grades_by_threshold(self):
        assert grade_momentum(20) == "A"
        assert grade_momentum(10) == "B"
        assert grade_momentum(4) == "C"
        assert grade_momentum(1) == "D"


# ---------------------------------------------------------------------------
# get_latest_earnings -- new yfinance-backed lookup replacing FMP's
# market-wide earning_calendar endpoint.
# ---------------------------------------------------------------------------


class TestGetLatestEarnings:
    def test_returns_most_recent_report_in_window(self):
        recent = pd.Timestamp(date.today() - timedelta(days=10))
        older = pd.Timestamp(date.today() - timedelta(days=100))
        df = pd.DataFrame(
            {"Reported EPS": [1.5, 1.2], "Surprise(%)": [12.0, 3.0]},
            index=pd.DatetimeIndex([recent, older], name="Earnings Date"),
        )
        with patch("track_earnings_momentum.yf.Ticker") as mock_ticker:
            mock_ticker.return_value = MagicMock(earnings_dates=df)
            result = get_latest_earnings("AAPL", lookback_days=30)

        assert result is not None
        earnings_date, surprise_pct = result
        assert earnings_date == recent.date().isoformat()
        assert surprise_pct == 12.0

    def test_excludes_reports_outside_lookback_window(self):
        older = pd.Timestamp(date.today() - timedelta(days=100))
        df = pd.DataFrame(
            {"Reported EPS": [1.2], "Surprise(%)": [3.0]},
            index=pd.DatetimeIndex([older], name="Earnings Date"),
        )
        with patch("track_earnings_momentum.yf.Ticker") as mock_ticker:
            mock_ticker.return_value = MagicMock(earnings_dates=df)
            assert get_latest_earnings("AAPL", lookback_days=30) is None

    def test_excludes_future_estimates_without_reported_eps(self):
        future = pd.Timestamp(date.today() + timedelta(days=5))
        df = pd.DataFrame(
            {"Reported EPS": [None], "Surprise(%)": [None]},
            index=pd.DatetimeIndex([future], name="Earnings Date"),
        )
        with patch("track_earnings_momentum.yf.Ticker") as mock_ticker:
            mock_ticker.return_value = MagicMock(earnings_dates=df)
            assert get_latest_earnings("AAPL", lookback_days=30) is None

    def test_empty_or_none_earnings_dates_returns_none(self):
        with patch("track_earnings_momentum.yf.Ticker") as mock_ticker:
            mock_ticker.return_value = MagicMock(earnings_dates=None)
            assert get_latest_earnings("AAPL", lookback_days=30) is None

    def test_exception_returns_none(self):
        with patch("track_earnings_momentum.yf.Ticker", side_effect=RuntimeError("boom")):
            assert get_latest_earnings("AAPL", lookback_days=30) is None
