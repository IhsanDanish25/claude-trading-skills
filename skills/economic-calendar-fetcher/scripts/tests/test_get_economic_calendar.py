"""Tests for get_economic_calendar.py"""

import io
import json
import os
import sys
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

# Add parent directory to path so we can import the script module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import get_economic_calendar as gec
from get_economic_calendar import (
    fetch_economic_calendar,
    format_event_output,
    get_api_key,
    validate_date_range,
)

# ---------------------------------------------------------------------------
# Sample fixtures
# ---------------------------------------------------------------------------

SAMPLE_EVENTS = [
    {
        "date": "2025-01-15 14:30:00",
        "country": "US",
        "event": "Consumer Price Index (CPI) YoY",
        "currency": "USD",
        "previous": 2.6,
        "estimate": 2.7,
        "actual": None,
        "change": None,
        "impact": "High",
        "changePercentage": None,
    },
    {
        "date": "2025-01-16 10:00:00",
        "country": "EU",
        "event": "ECB Interest Rate Decision",
        "currency": "EUR",
        "previous": 4.5,
        "estimate": 4.5,
        "actual": None,
        "change": None,
        "impact": "High",
        "changePercentage": None,
    },
]


# ---------------------------------------------------------------------------
# get_api_key tests
# ---------------------------------------------------------------------------


class TestGetApiKey:
    def test_returns_key_when_set(self, monkeypatch):
        monkeypatch.setenv("FMP_API_KEY", "test_key_123")
        assert get_api_key() == "test_key_123"

    def test_returns_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        assert get_api_key() is None


# ---------------------------------------------------------------------------
# validate_date_range tests
# ---------------------------------------------------------------------------


class TestValidateDateRange:
    def test_valid_range(self):
        validate_date_range("2025-01-01", "2025-01-31")

    def test_same_day(self):
        validate_date_range("2025-06-15", "2025-06-15")

    def test_max_90_days(self):
        validate_date_range("2025-01-01", "2025-03-31")  # 89 days

    def test_exceeds_90_days(self):
        with pytest.raises(ValueError, match="exceeds maximum of 90 days"):
            validate_date_range("2025-01-01", "2025-06-01")

    def test_start_after_end(self):
        with pytest.raises(ValueError, match="after end date"):
            validate_date_range("2025-03-01", "2025-01-01")

    def test_invalid_date_format(self):
        with pytest.raises(ValueError, match="Invalid date format"):
            validate_date_range("01-01-2025", "2025-01-31")

    def test_invalid_date_value(self):
        with pytest.raises(ValueError, match="Invalid date format"):
            validate_date_range("2025-13-01", "2025-14-01")

    def test_past_dates_warns(self, capsys):
        past = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        past_end = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
        validate_date_range(past, past_end)
        captured = capsys.readouterr()
        assert "in the past" in captured.err


# ---------------------------------------------------------------------------
# format_event_output tests
# ---------------------------------------------------------------------------


class TestFormatEventOutput:
    def test_json_format_roundtrip(self):
        output = format_event_output(SAMPLE_EVENTS, "json")
        parsed = json.loads(output)
        assert len(parsed) == 2
        assert parsed[0]["event"] == "Consumer Price Index (CPI) YoY"

    def test_json_empty_list(self):
        output = format_event_output([], "json")
        assert json.loads(output) == []

    def test_text_format_header(self):
        output = format_event_output(SAMPLE_EVENTS, "text")
        assert "Total: 2" in output

    def test_text_format_contains_event_name(self):
        output = format_event_output(SAMPLE_EVENTS, "text")
        assert "Consumer Price Index (CPI) YoY" in output
        assert "ECB Interest Rate Decision" in output

    def test_text_format_shows_previous(self):
        output = format_event_output(SAMPLE_EVENTS, "text")
        assert "Previous: 2.6" in output

    def test_text_format_omits_none_actual(self):
        output = format_event_output(SAMPLE_EVENTS, "text")
        assert "Actual:" not in output

    def test_text_format_shows_actual_when_present(self):
        events = [
            {
                "date": "2025-01-10 14:30:00",
                "country": "US",
                "event": "NFP",
                "currency": "USD",
                "previous": 200,
                "estimate": 210,
                "actual": 256,
                "change": 56,
                "impact": "High",
                "changePercentage": 28.0,
            }
        ]
        output = format_event_output(events, "text")
        assert "Actual: 256" in output
        assert "Change: 56" in output
        assert "Change %: 28.0%" in output

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError, match="Unknown output format"):
            format_event_output([], "csv")


# ---------------------------------------------------------------------------
# fetch_economic_calendar tests
#
# A 404 from FMP's stable API means the endpoint/plan doesn't support the
# request -- not that the calendar is confirmed empty. It must surface as a
# real error, not get silently swallowed into a "0 events" result (which
# would be indistinguishable from a genuinely quiet week in the dashboard).
# ---------------------------------------------------------------------------


class TestFetchEconomicCalendar:
    def test_404_raises_instead_of_returning_empty(self):
        def raise_404(*args, **kwargs):
            raise urllib.error.HTTPError(
                "https://financialmodelingprep.com/stable/economics-calendar",
                404, "Not Found", {}, io.BytesIO(b"[]"),
            )

        with patch("urllib.request.urlopen", side_effect=raise_404):
            with pytest.raises(urllib.error.HTTPError):
                fetch_economic_calendar("2025-01-01", "2025-01-07", "fake_key")

    def test_non_404_http_error_includes_response_body(self):
        def raise_401(*args, **kwargs):
            raise urllib.error.HTTPError(
                "https://financialmodelingprep.com/stable/economics-calendar",
                401, "Unauthorized", {}, io.BytesIO(b'{"error": "Invalid API key"}'),
            )

        with patch("urllib.request.urlopen", side_effect=raise_401):
            with pytest.raises(urllib.error.HTTPError, match="Invalid API key"):
                fetch_economic_calendar("2025-01-01", "2025-01-07", "fake_key")


# ---------------------------------------------------------------------------
# main() error-handling tests
#
# A fetch failure must exit non-zero, not fake a successful empty result --
# generate_dashboard.py's skill_status/health-note handling relies on the
# exit code to tell "fetch failed" apart from "genuinely 0 events".
# ---------------------------------------------------------------------------


class TestMainErrorHandling:
    def test_fetch_failure_exits_nonzero_and_writes_nothing(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("FMP_API_KEY", "fake_key")
        output_path = tmp_path / "economic_calendar_latest.json"
        monkeypatch.setattr(sys, "argv", ["get_economic_calendar.py", "--output", str(output_path)])
        monkeypatch.setattr(
            gec, "fetch_economic_calendar",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("Network error: simulated failure")),
        )

        with pytest.raises(SystemExit) as exc_info:
            gec.main()

        assert exc_info.value.code == 1
        assert not output_path.exists()
        assert "economic calendar fetch failed" in capsys.readouterr().err

    def test_404_exits_with_distinct_code_not_generic_failure(self, monkeypatch, tmp_path, capsys):
        """A 404 means "not on this FMP plan", not a transient fetch failure --
        it must exit 2 (not 1) so generate_dashboard.py can render it as a
        calm status instead of a red "scan failed" alarm.
        """
        monkeypatch.setenv("FMP_API_KEY", "fake_key")
        output_path = tmp_path / "economic_calendar_latest.json"
        monkeypatch.setattr(sys, "argv", ["get_economic_calendar.py", "--output", str(output_path)])

        def raise_404(*args, **kwargs):
            raise urllib.error.HTTPError(
                "https://financialmodelingprep.com/stable/economics-calendar",
                404, "Not Found", {}, io.BytesIO(b"[]"),
            )

        monkeypatch.setattr(gec, "fetch_economic_calendar", raise_404)

        with pytest.raises(SystemExit) as exc_info:
            gec.main()

        assert exc_info.value.code == 2
        assert not output_path.exists()
        assert "not available on the current FMP plan" in capsys.readouterr().err
