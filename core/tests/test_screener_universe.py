"""Tests for core.fmp.get_screener_universe using /stable/company-screener."""

from unittest.mock import patch, MagicMock
import pytest

from core.fmp import get_screener_universe


SAMPLE_SCREENER_RESPONSE = [
    {"symbol": "AAPL", "companyName": "Apple Inc.", "marketCap": 3000000000000},
    {"symbol": "MSFT", "companyName": "Microsoft Corp.", "marketCap": 2800000000000},
    {"symbol": "NVDA", "companyName": "NVIDIA Corp.", "marketCap": 2500000000000},
]


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset module-level caches between tests."""
    import core.fmp as mod
    mod._cache.clear()
    mod._warned_unavailable.clear()


@patch("core.fmp.FMP_API_KEY", "test-key")
@patch("core.fmp.requests.get")
def test_returns_symbol_list(mock_get):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = SAMPLE_SCREENER_RESPONSE
    resp.raise_for_status = MagicMock()
    mock_get.return_value = resp

    result = get_screener_universe()

    assert result == ["AAPL", "MSFT", "NVDA"]


@patch("core.fmp.FMP_API_KEY", "test-key")
@patch("core.fmp.requests.get")
def test_passes_correct_params(mock_get):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = SAMPLE_SCREENER_RESPONSE
    resp.raise_for_status = MagicMock()
    mock_get.return_value = resp

    get_screener_universe(min_market_cap=5_000_000_000, min_volume=1_000_000, limit=200)

    call_args = mock_get.call_args
    url = call_args[0][0]
    params = call_args[1].get("params") or call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
    assert "/stable/company-screener" in url
    assert params["marketCapMoreThan"] == 5_000_000_000
    assert params["volumeMoreThan"] == 1_000_000
    assert params["limit"] == 200
    assert params["isActivelyTrading"] == "true"
    assert params["isEtf"] == "false"


@patch("core.fmp.FMP_API_KEY", "test-key")
@patch("core.fmp.requests.get")
def test_returns_empty_on_api_error(mock_get):
    mock_get.side_effect = Exception("Connection error")

    result = get_screener_universe()

    assert result == []


@patch("core.fmp.FMP_API_KEY", "")
def test_returns_empty_when_no_api_key():
    result = get_screener_universe()

    assert result == []


@patch("core.fmp.FMP_API_KEY", "test-key")
@patch("core.fmp.requests.get")
def test_skips_rows_without_symbol(mock_get):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {"symbol": "AAPL", "companyName": "Apple"},
        {"companyName": "No Symbol Corp"},
        {"symbol": "MSFT", "companyName": "Microsoft"},
    ]
    resp.raise_for_status = MagicMock()
    mock_get.return_value = resp

    result = get_screener_universe()

    assert result == ["AAPL", "MSFT"]


@patch("core.fmp.FMP_API_KEY", "test-key")
@patch("core.fmp.requests.get")
def test_returns_empty_on_non_list_response(mock_get):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"error": "not authorized"}
    resp.raise_for_status = MagicMock()
    mock_get.return_value = resp

    result = get_screener_universe()

    assert result == []
