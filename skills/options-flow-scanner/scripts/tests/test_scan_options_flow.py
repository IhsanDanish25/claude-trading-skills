"""Tests for scan_options_flow.py"""

import math
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scan_options_flow import _row_to_dict, _safe_int, score_contract


class TestSafeInt:
    def test_plain_int_passthrough(self):
        assert _safe_int(5) == 5

    def test_float_truncates(self):
        assert _safe_int(5.9) == 5

    def test_none_returns_default(self):
        assert _safe_int(None) == 0
        assert _safe_int(None, default=7) == 7

    def test_nan_returns_default_instead_of_raising(self):
        """Regression test: yfinance option chains report NaN volume/openInterest
        for illiquid far-OTM contracts. `int(nan)` raises ValueError, and since
        NaN is truthy in Python, a plain `value or 0` guard doesn't catch it --
        this used to abort the whole symbol's scan on the first such contract.
        """
        assert _safe_int(float("nan")) == 0

    def test_unparseable_string_returns_default(self):
        assert _safe_int("not a number") == 0


class TestRowToDict:
    def test_nan_volume_and_open_interest_become_zero(self):
        row = SimpleNamespace(
            strike=150.0, volume=float("nan"), openInterest=float("nan"),
            impliedVolatility=0.35, lastPrice=2.5, inTheMoney=False,
        )
        d = _row_to_dict("AAPL", row, "CALL", "2026-01-16", 30)
        assert d["volume"] == 0
        assert d["open_interest"] == 0

    def test_normal_row_converts_cleanly(self):
        row = SimpleNamespace(
            strike=150.0, volume=1200, openInterest=300,
            impliedVolatility=0.35, lastPrice=2.5, inTheMoney=True,
        )
        d = _row_to_dict("AAPL", row, "PUT", "2026-01-16", 30)
        assert d["volume"] == 1200
        assert d["open_interest"] == 300
        assert d["option_type"] == "PUT"


class TestScoreContract:
    def test_below_min_volume_returns_none(self):
        c = {"volume": 10, "open_interest": 100, "implied_volatility": 0.3, "dte": 20,
             "option_type": "CALL", "symbol": "AAPL", "strike": 150, "expiry": "2026-01-16",
             "last_price": 2.0, "in_the_money": False}
        assert score_contract(c, min_volume=100, min_oi_ratio=1.5) is None

    def test_high_vol_oi_ratio_flags_and_scores(self):
        c = {"volume": 5000, "open_interest": 500, "implied_volatility": 0.6, "dte": 20,
             "option_type": "CALL", "symbol": "AAPL", "strike": 150, "expiry": "2026-01-16",
             "last_price": 2.0, "in_the_money": False}
        scored = score_contract(c, min_volume=100, min_oi_ratio=1.5)
        assert scored is not None
        assert scored["vol_oi_ratio"] == 10.0
        assert scored["signal"] == "CALL_SWEEP"
        assert not math.isnan(scored["score"])
