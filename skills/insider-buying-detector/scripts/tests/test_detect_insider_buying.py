"""Tests for detect_insider_buying.py"""

import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detect_insider_buying import fetch_insider, score_transactions

# ---------------------------------------------------------------------------
# fetch_insider tests
#
# yfinance's own "Transaction" column on insider_transactions is blank/
# unreliable; the actual transaction type only shows up as free text in the
# "Text" column ("Purchase at price 21.12 per share.", "Sale at price ...",
# "Stock Award(Grant) at price ..."). fetch_insider must filter on that.
# ---------------------------------------------------------------------------


def _fake_df(rows):
    return pd.DataFrame(rows)


class TestFetchInsider:
    def test_filters_to_purchases_only(self):
        recent = pd.Timestamp(date.today() - timedelta(days=5))
        df = _fake_df([
            {"Insider": "COHEN RYAN", "Position": "CEO", "Text": "Purchase at price 21.12 per share.",
             "Shares": 1000, "Value": 21120.0, "Start Date": recent},
            {"Insider": "SMITH JANE", "Position": "Director", "Text": "Sale at price 30.00 per share.",
             "Shares": 500, "Value": 15000.0, "Start Date": recent},
            {"Insider": "DOE JOHN", "Position": "Officer", "Text": "Stock Award(Grant) at price 0.00 per share.",
             "Shares": 2000, "Value": 0.0, "Start Date": recent},
        ])
        with patch("detect_insider_buying.yf.Ticker") as mock_ticker:
            mock_ticker.return_value = MagicMock(insider_transactions=df)
            out = fetch_insider("GME", days=30)

        assert len(out) == 1
        assert out[0]["reportingName"] == "COHEN RYAN"
        assert out[0]["securitiesTransacted"] == 1000
        assert out[0]["price"] == pytest.approx(21.12)

    def test_excludes_purchases_outside_lookback_window(self):
        old = pd.Timestamp(date.today() - timedelta(days=400))
        df = _fake_df([
            {"Insider": "COHEN RYAN", "Position": "CEO", "Text": "Purchase at price 21.12 per share.",
             "Shares": 1000, "Value": 21120.0, "Start Date": old},
        ])
        with patch("detect_insider_buying.yf.Ticker") as mock_ticker:
            mock_ticker.return_value = MagicMock(insider_transactions=df)
            out = fetch_insider("GME", days=30)

        assert out == []

    def test_nan_value_does_not_crash_price_calc(self):
        recent = pd.Timestamp(date.today() - timedelta(days=5))
        df = _fake_df([
            {"Insider": "COHEN RYAN", "Position": "CEO", "Text": "Purchase at price 21.12 per share.",
             "Shares": 1000, "Value": float("nan"), "Start Date": recent},
        ])
        with patch("detect_insider_buying.yf.Ticker") as mock_ticker:
            mock_ticker.return_value = MagicMock(insider_transactions=df)
            out = fetch_insider("GME", days=30)

        assert len(out) == 1
        assert out[0]["price"] == 0.0

    def test_empty_dataframe_returns_empty_list(self):
        with patch("detect_insider_buying.yf.Ticker") as mock_ticker:
            mock_ticker.return_value = MagicMock(insider_transactions=pd.DataFrame())
            out = fetch_insider("AAPL", days=30)
        assert out == []

    def test_yfinance_exception_returns_empty_list(self):
        with patch("detect_insider_buying.yf.Ticker", side_effect=RuntimeError("boom")):
            out = fetch_insider("AAPL", days=30)
        assert out == []


# ---------------------------------------------------------------------------
# score_transactions tests (unchanged scoring logic, still exercised through
# the fetch_insider output shape)
# ---------------------------------------------------------------------------


class TestScoreTransactions:
    def test_no_transactions_returns_none(self):
        assert score_transactions("AAPL", []) is None

    def test_single_ceo_purchase_scores_and_grades(self):
        txns = [{
            "reportingName": "COHEN RYAN", "typeOfOwner": "CEO",
            "securitiesTransacted": 1_000_000, "price": 21.12,
            "filingDate": "2026-01-21",
        }]
        r = score_transactions("GME", txns)
        assert r["symbol"] == "GME"
        assert r["unique_insiders"] == 1
        assert r["largest_buyer"] == "COHEN RYAN"
        assert r["grade"] in ("A", "B", "C", "D")
