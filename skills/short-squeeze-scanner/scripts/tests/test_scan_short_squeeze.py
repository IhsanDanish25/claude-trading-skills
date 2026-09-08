"""Tests for scan_short_squeeze.py"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scan_short_squeeze import fetch_snapshot, score_squeeze


class TestScoreSqueeze:
    def test_below_min_short_float_returns_none(self):
        info = {"shortPercentOfFloat": 0.02, "shortRatio": 5.0,
                "currentPrice": 20.0, "previousClose": 19.0,
                "volume": 1_000_000, "averageVolume": 500_000}
        assert score_squeeze("XYZ", info, min_short_float=10.0, min_dtc=3.0) is None

    def test_below_min_days_to_cover_returns_none(self):
        info = {"shortPercentOfFloat": 0.15, "shortRatio": 1.0,
                "currentPrice": 20.0, "previousClose": 19.0,
                "volume": 1_000_000, "averageVolume": 500_000}
        assert score_squeeze("XYZ", info, min_short_float=10.0, min_dtc=3.0) is None

    def test_qualifying_setup_scores_and_grades(self):
        info = {"shortPercentOfFloat": 0.20, "shortRatio": 6.0,
                "currentPrice": 22.0, "previousClose": 20.0,
                "volume": 2_000_000, "averageVolume": 500_000}
        r = score_squeeze("XYZ", info, min_short_float=10.0, min_dtc=3.0)
        assert r is not None
        assert r["symbol"] == "XYZ"
        assert r["short_float_pct"] == 20.0
        assert r["days_to_cover"] == 6.0
        assert r["grade"] in ("A", "B", "C", "D")

    def test_short_float_already_in_percent_form_not_double_scaled(self):
        # yfinance sometimes returns shortPercentOfFloat as a plain percent
        # (e.g. 20.0) rather than a fraction (0.20) -- both must resolve the
        # same way since score_squeeze branches on `< 1`.
        info = {"shortPercentOfFloat": 20.0, "shortRatio": 6.0,
                "currentPrice": 22.0, "previousClose": 20.0,
                "volume": 2_000_000, "averageVolume": 500_000}
        r = score_squeeze("XYZ", info, min_short_float=10.0, min_dtc=3.0)
        assert r["short_float_pct"] == 20.0


class TestFetchSnapshot:
    def test_missing_price_returns_none(self):
        with patch("scan_short_squeeze.yf.Ticker") as mock_ticker:
            mock_ticker.return_value = MagicMock(info={})
            assert fetch_snapshot("XYZ") is None

    def test_exception_returns_none(self):
        with patch("scan_short_squeeze.yf.Ticker", side_effect=RuntimeError("boom")):
            assert fetch_snapshot("XYZ") is None

    def test_valid_info_passes_through(self):
        info = {"currentPrice": 20.0, "shortPercentOfFloat": 0.2}
        with patch("scan_short_squeeze.yf.Ticker") as mock_ticker:
            mock_ticker.return_value = MagicMock(info=info)
            assert fetch_snapshot("XYZ") == info
