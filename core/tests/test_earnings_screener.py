"""Unit tests for core.earnings_screener — bulk FMP path + per-symbol fallback.

Pure-logic tests: network calls are mocked, no live FMP/Alpaca/yfinance access.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

from core import earnings_screener as es


def _sp500(monkeypatch):
    monkeypatch.setattr(es, "get_sp500_symbols", lambda: ["AAPL", "MSFT", "NVDA"])


def test_bulk_path_used_when_fmp_available(monkeypatch):
    """screen_earnings should call the bulk FMP endpoint exactly once, not
    per-symbol, when FMP_API_KEY is set and the request succeeds."""
    _sp500(monkeypatch)
    calendar_calls = []

    def fake_calendar(start_iso, end_iso):
        calendar_calls.append((start_iso, end_iso))
        return [
            {"symbol": "AAPL", "date": "2026-06-28", "eps": 2.20, "epsEstimated": 2.00},
            {"symbol": "MSFT", "date": "2026-06-27", "eps": 1.00, "epsEstimated": 1.05},  # miss
            {
                "symbol": "ZZZZ",
                "date": "2026-06-28",
                "eps": 5.00,
                "epsEstimated": 1.00,
            },  # not in universe
        ]

    monkeypatch.setattr(es, "_fetch_fmp_earnings_calendar", fake_calendar)
    monkeypatch.setattr(
        es,
        "_liquidity",
        lambda syms: {
            "AAPL": {"price": 200.0, "avg_volume": 10_000_000},
        },
    )

    with patch("backtest_harness.earnings_data.get_symbol_earnings") as legacy_fetch:
        candidates = es.screen_earnings(
            as_of=datetime.date(2026, 7, 1),
            lookback_days=7,
            min_surprise_pct=5.0,
        )
        legacy_fetch.assert_not_called()

    assert len(calendar_calls) == 1  # one bulk call, not one per symbol
    symbols = [c["symbol"] for c in candidates]
    assert symbols == ["AAPL"]  # MSFT misses (surprise < 5%), ZZZZ outside universe


def test_falls_back_to_per_symbol_scan_when_fmp_unavailable(monkeypatch):
    """When the bulk FMP call fails (e.g. no API key), screen_earnings should
    fall back to the legacy per-symbol disk-cached scan."""
    _sp500(monkeypatch)
    monkeypatch.setattr(es, "_fetch_fmp_earnings_calendar", lambda start, end: None)
    monkeypatch.setattr(
        es,
        "_liquidity",
        lambda syms: {
            "AAPL": {"price": 200.0, "avg_volume": 10_000_000},
        },
    )

    def fake_get_symbol_earnings(sym):
        if sym == "AAPL":
            return [
                {
                    "date": "2026-06-28",
                    "reported_eps": 2.20,
                    "eps_estimate": 2.00,
                    "surprise_pct": 10.0,
                }
            ]
        return []

    with patch(
        "backtest_harness.earnings_data.get_symbol_earnings", side_effect=fake_get_symbol_earnings
    ) as legacy_fetch:
        candidates = es.screen_earnings(
            as_of=datetime.date(2026, 7, 1),
            lookback_days=7,
            min_surprise_pct=5.0,
        )
        assert legacy_fetch.call_count == 3  # one per S&P symbol in the fixture

    assert [c["symbol"] for c in candidates] == ["AAPL"]


def test_compute_surprise_pct_handles_missing_values():
    assert es.compute_surprise_pct(None, 1.0) is None
    assert es.compute_surprise_pct(1.0, None) is None
    assert es.compute_surprise_pct(1.0, 0) is None
    assert es.compute_surprise_pct(2.0, 1.0) == 100.0
