"""Test configuration and shared fixtures for buffett-value scripts."""

import os
import sys

import pytest

# Add scripts/ directory to sys.path so hold/sell/analyst/buy can be imported.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def make_entry_snapshot(
    symbol: str = "AAPL",
    conviction_score: float = 0.8,
    roe_score: float = 1.0,
    debt_score: float = 0.9,
    business_quality_passed: bool = True,
    graham_number: float = 220.0,
    graham_ratio: float = 1.4,
) -> dict:
    """Build a candidate dict shaped like analyst.analyze_symbol's output."""
    return {
        "symbol": symbol,
        "conviction_score": conviction_score,
        "criteria_met": {"business_quality": business_quality_passed},
        "fundamentals": {
            "business_quality": {
                "score": 0.9,
                "details": {
                    "ROE": {"score": roe_score},
                    "Debt/Equity": {"score": debt_score},
                },
            },
        },
        "valuation": {
            "valuation_details": {
                "graham_number": graham_number,
                "graham_ratio": graham_ratio,
            },
        },
    }


@pytest.fixture()
def entry_snapshot() -> dict:
    return make_entry_snapshot()
