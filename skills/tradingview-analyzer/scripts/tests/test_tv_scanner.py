"""Tests for tv_scanner.py — TradingView indicator fetcher and screener."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tv_scanner import (
    DEFAULT_EXCHANGE,
    DEFAULT_SCREENER,
    INTERVAL_MAP,
    KEY_INDICATORS,
    SCAN_PRESETS,
    fetch_analysis,
    fetch_multi,
    format_markdown_report,
    main,
    parse_args,
    run_scan,
    write_outputs,
)


def _mock_analysis(
    recommendation: str = "BUY",
    buy: int = 15,
    sell: int = 3,
    neutral: int = 8,
    rsi: float = 45.0,
    close: float = 150.0,
    sma50: float = 145.0,
    sma200: float = 140.0,
    macd: float = 1.5,
    macd_signal: float = 1.0,
    stoch_k: float = 55.0,
) -> MagicMock:
    """Create a mock TA_Handler.get_analysis() return value."""
    mock = MagicMock()
    mock.summary = {"RECOMMENDATION": recommendation, "BUY": buy, "SELL": sell, "NEUTRAL": neutral}
    mock.oscillators = {
        "RECOMMENDATION": recommendation,
        "BUY": buy,
        "SELL": sell,
        "NEUTRAL": neutral,
        "COMPUTE": {"RSI": 1, "MACD": 1, "Stoch.K": 0},
    }
    mock.moving_averages = {
        "RECOMMENDATION": recommendation,
        "BUY": buy,
        "SELL": sell,
        "NEUTRAL": neutral,
        "COMPUTE": {"SMA10": 1, "EMA10": 1},
    }
    mock.indicators = {
        "RSI": rsi,
        "close": close,
        "open": 148.0,
        "high": 152.0,
        "low": 147.0,
        "volume": 50000000,
        "change": 1.35,
        "SMA50": sma50,
        "SMA200": sma200,
        "SMA10": 149.0,
        "SMA20": 147.0,
        "SMA100": 142.0,
        "EMA10": 149.5,
        "EMA20": 147.5,
        "EMA50": 145.5,
        "EMA100": 142.5,
        "EMA200": 140.5,
        "MACD.macd": macd,
        "MACD.signal": macd_signal,
        "Stoch.K": stoch_k,
        "Stoch.D": 53.0,
        "ADX": 22.0,
        "CCI20": 50.0,
        "AO": 3.5,
        "Mom": 5.0,
        "W.R": -45.0,
        "UO": 55.0,
        "BB.upper": 155.0,
        "BB.lower": 145.0,
        "Pivot.M.Classic.Middle": 150.0,
        "Pivot.M.Classic.R1": 155.0,
        "Pivot.M.Classic.S1": 145.0,
    }
    return mock


class TestParseArgs:
    def test_default_values(self):
        args = parse_args(["--symbols", "AAPL"])
        assert args.symbols == "AAPL"
        assert args.exchange == DEFAULT_EXCHANGE
        assert args.interval == "1d"
        assert args.format == "both"
        assert args.scan is None

    def test_multiple_symbols(self):
        args = parse_args(["--symbols", "AAPL,MSFT,GOOGL"])
        assert args.symbols == "AAPL,MSFT,GOOGL"

    def test_scan_mode(self):
        args = parse_args(["--symbols", "AAPL", "--scan", "oversold"])
        assert args.scan == "oversold"

    def test_all_intervals_valid(self):
        for interval in INTERVAL_MAP:
            args = parse_args(["--symbols", "AAPL", "--interval", interval])
            assert args.interval == interval

    def test_custom_output_dir(self):
        args = parse_args(["--symbols", "AAPL", "--output-dir", "/tmp/out"])
        assert args.output_dir == "/tmp/out"


class TestFetchAnalysis:
    @patch("tv_scanner.TA_Handler")
    def test_returns_structured_data(self, mock_handler_cls):
        mock_instance = MagicMock()
        mock_instance.get_analysis.return_value = _mock_analysis()
        mock_handler_cls.return_value = mock_instance

        result = fetch_analysis("AAPL", "NASDAQ", "america", "1d")

        assert result["symbol"] == "AAPL"
        assert result["exchange"] == "NASDAQ"
        assert result["interval"] == "1d"
        assert result["summary"]["RECOMMENDATION"] == "BUY"
        assert "RSI" in result["indicators"]
        assert "timestamp" in result

    @patch("tv_scanner.TA_Handler")
    def test_uppercases_symbol_and_exchange(self, mock_handler_cls):
        mock_instance = MagicMock()
        mock_instance.get_analysis.return_value = _mock_analysis()
        mock_handler_cls.return_value = mock_instance

        result = fetch_analysis("aapl", "nasdaq")
        assert result["symbol"] == "AAPL"
        assert result["exchange"] == "NASDAQ"

    def test_invalid_interval_raises(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            fetch_analysis("AAPL", interval="3h")

    @patch("tv_scanner.TA_Handler")
    def test_rounds_float_indicators(self, mock_handler_cls):
        mock_instance = MagicMock()
        mock_instance.get_analysis.return_value = _mock_analysis(rsi=45.123456789)
        mock_handler_cls.return_value = mock_instance

        result = fetch_analysis("AAPL")
        assert result["indicators"]["RSI"] == 45.1235


class TestFetchMulti:
    @patch("tv_scanner.TA_Handler")
    def test_fetches_all_symbols(self, mock_handler_cls):
        mock_instance = MagicMock()
        mock_instance.get_analysis.return_value = _mock_analysis()
        mock_handler_cls.return_value = mock_instance

        results = fetch_multi(["AAPL", "MSFT"])
        assert len(results) == 2
        assert results[0]["symbol"] == "AAPL"
        assert results[1]["symbol"] == "MSFT"

    @patch("tv_scanner.TA_Handler")
    def test_skips_failed_symbols(self, mock_handler_cls):
        mock_instance = MagicMock()
        call_count = 0

        def side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("Network error")
            return _mock_analysis()

        mock_instance.get_analysis.side_effect = side_effect
        mock_handler_cls.return_value = mock_instance

        results = fetch_multi(["AAPL", "BAD", "GOOGL"])
        assert len(results) == 2


class TestRunScan:
    @patch("tv_scanner.TA_Handler")
    def test_oversold_filter(self, mock_handler_cls):
        mock_instance = MagicMock()
        mock_instance.get_analysis.side_effect = [
            _mock_analysis(rsi=25.0),
            _mock_analysis(rsi=55.0),
            _mock_analysis(rsi=20.0),
        ]
        mock_handler_cls.return_value = mock_instance

        results = run_scan("oversold", ["A", "B", "C"])
        assert len(results) == 2
        assert results[0]["indicators"]["RSI"] < results[1]["indicators"]["RSI"]

    @patch("tv_scanner.TA_Handler")
    def test_strong_buy_filter(self, mock_handler_cls):
        mock_instance = MagicMock()
        mock_instance.get_analysis.side_effect = [
            _mock_analysis(recommendation="STRONG_BUY"),
            _mock_analysis(recommendation="SELL"),
            _mock_analysis(recommendation="STRONG_BUY"),
        ]
        mock_handler_cls.return_value = mock_instance

        results = run_scan("strong_buy", ["A", "B", "C"])
        assert len(results) == 2

    @patch("tv_scanner.TA_Handler")
    def test_trending_up_filter(self, mock_handler_cls):
        mock_instance = MagicMock()
        mock_instance.get_analysis.side_effect = [
            _mock_analysis(close=150.0, sma50=145.0, sma200=140.0),
            _mock_analysis(close=130.0, sma50=145.0, sma200=140.0),
        ]
        mock_handler_cls.return_value = mock_instance

        results = run_scan("trending_up", ["A", "B"])
        assert len(results) == 1

    @patch("tv_scanner.TA_Handler")
    def test_top_limit(self, mock_handler_cls):
        mock_instance = MagicMock()
        mock_instance.get_analysis.return_value = _mock_analysis(rsi=10.0)
        mock_handler_cls.return_value = mock_instance

        results = run_scan("oversold", ["A", "B", "C", "D", "E"], top=2)
        assert len(results) == 2

    def test_invalid_preset_raises(self):
        with pytest.raises(ValueError, match="Unknown scan preset"):
            run_scan("nonexistent", ["AAPL"])


class TestFormatMarkdownReport:
    @patch("tv_scanner.TA_Handler")
    def test_contains_key_sections(self, mock_handler_cls):
        mock_instance = MagicMock()
        mock_instance.get_analysis.return_value = _mock_analysis()
        mock_handler_cls.return_value = mock_instance

        results = fetch_multi(["AAPL"])
        md = format_markdown_report(results, "Test Report")

        assert "# Test Report" in md
        assert "AAPL" in md
        assert "Recommendation:" in md
        assert "Oscillators" in md
        assert "Moving Averages" in md
        assert "Key Readings" in md
        assert "RSI(14):" in md
        assert "MACD:" in md

    def test_empty_results(self):
        md = format_markdown_report([], "Empty")
        assert "# Empty" in md


class TestWriteOutputs:
    @patch("tv_scanner.TA_Handler")
    def test_writes_json_and_md(self, mock_handler_cls, tmp_path):
        mock_instance = MagicMock()
        mock_instance.get_analysis.return_value = _mock_analysis()
        mock_handler_cls.return_value = mock_instance

        results = fetch_multi(["AAPL"])
        paths = write_outputs(results, "Test", "test_output", tmp_path, "both")

        assert len(paths) == 2
        assert (tmp_path / "test_output.json").exists()
        assert (tmp_path / "test_output.md").exists()

        data = json.loads((tmp_path / "test_output.json").read_text())
        assert len(data) == 1
        assert data[0]["symbol"] == "AAPL"

    @patch("tv_scanner.TA_Handler")
    def test_json_only(self, mock_handler_cls, tmp_path):
        mock_instance = MagicMock()
        mock_instance.get_analysis.return_value = _mock_analysis()
        mock_handler_cls.return_value = mock_instance

        results = fetch_multi(["AAPL"])
        paths = write_outputs(results, "Test", "test_output", tmp_path, "json")

        assert len(paths) == 1
        assert (tmp_path / "test_output.json").exists()
        assert not (tmp_path / "test_output.md").exists()

    def test_creates_output_dir(self, tmp_path):
        new_dir = tmp_path / "nested" / "dir"
        write_outputs([], "Test", "test", new_dir, "json")
        assert new_dir.exists()


class TestMainCLI:
    @patch("tv_scanner.TA_Handler")
    def test_symbols_mode(self, mock_handler_cls, tmp_path):
        mock_instance = MagicMock()
        mock_instance.get_analysis.return_value = _mock_analysis()
        mock_handler_cls.return_value = mock_instance

        rc = main(["--symbols", "AAPL", "--output-dir", str(tmp_path)])
        assert rc == 0

    @patch("tv_scanner.TA_Handler")
    def test_scan_mode(self, mock_handler_cls, tmp_path):
        mock_instance = MagicMock()
        mock_instance.get_analysis.return_value = _mock_analysis(rsi=25.0)
        mock_handler_cls.return_value = mock_instance

        rc = main(["--symbols", "AAPL", "--scan", "oversold", "--output-dir", str(tmp_path)])
        assert rc == 0

    def test_no_args_returns_1(self):
        rc = main([])
        assert rc == 1

    @patch("tv_scanner.TA_Handler")
    def test_scan_without_symbols_returns_1(self, mock_handler_cls):
        rc = main(["--scan", "oversold"])
        assert rc == 1

    @patch("tv_scanner.TA_Handler")
    def test_all_fail_returns_1(self, mock_handler_cls, tmp_path):
        mock_instance = MagicMock()
        mock_instance.get_analysis.side_effect = Exception("fail")
        mock_handler_cls.return_value = mock_instance

        rc = main(["--symbols", "BAD", "--output-dir", str(tmp_path)])
        assert rc == 1


class TestConstants:
    def test_all_scan_presets_have_description(self):
        for name, config in SCAN_PRESETS.items():
            assert "description" in config, f"Preset {name} missing description"

    def test_all_scan_presets_have_filter(self):
        for name, config in SCAN_PRESETS.items():
            assert "filter_fn" in config or "recommendation" in config, (
                f"Preset {name} needs filter_fn or recommendation"
            )

    def test_key_indicators_not_empty(self):
        assert len(KEY_INDICATORS) > 10

    def test_interval_map_covers_all_timeframes(self):
        assert "1d" in INTERVAL_MAP
        assert "1W" in INTERVAL_MAP
        assert "1M" in INTERVAL_MAP
        assert "1h" in INTERVAL_MAP
