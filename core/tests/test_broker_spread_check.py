"""Unit tests for BrokerClient.check_spread() / check_crypto_spread() — the
hard pre-trade spread gate (reject entry when bid-ask spread exceeds
MAX_SPREAD_PCT of price) and its wiring into buy().

Covers the required scenarios: missing bid/ask data, zero spread, a
confirmed-closed market, and an API error/timeout during the check.

Pure-logic tests: BrokerClient is built via object.__new__ to skip
TradingClient/StockHistoricalDataClient construction (no network, no API
keys), mirroring the pattern in test_broker_buy_cash_cap.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.broker import BrokerClient


def _quote(bid, ask):
    return SimpleNamespace(bid_price=bid, ask_price=ask)


class _FakeDataClient:
    def __init__(self, quotes: dict):
        self._quotes = quotes

    def get_stock_latest_quote(self, req):
        symbol = req.symbol_or_symbols
        if isinstance(symbol, list):
            symbol = symbol[0]
        return {symbol: self._quotes[symbol]}


class _RaisingDataClient:
    def get_stock_latest_quote(self, req):
        raise TimeoutError("simulated API timeout")


class _FakeTradeClient:
    def __init__(self, is_open: bool = True, raise_on_clock: bool = False):
        self._is_open = is_open
        self._raise_on_clock = raise_on_clock

    def get_clock(self):
        if self._raise_on_clock:
            raise ConnectionError("simulated clock API failure")
        return SimpleNamespace(is_open=self._is_open)


def _make_broker(data_client, trade_client) -> BrokerClient:
    b = object.__new__(BrokerClient)
    b.data = data_client
    b.trade = trade_client
    return b


class TestCheckSpread:
    def test_zero_spread_passes(self):
        b = _make_broker(
            _FakeDataClient({"AAPL": _quote(100.0, 100.0)}),
            _FakeTradeClient(is_open=True),
        )
        result = b.check_spread("AAPL")
        assert result["ok"] is True
        assert result["spread_pct"] == 0.0

    def test_narrow_spread_passes(self):
        # spread = 0.5% of mid, under the 2% default MAX_SPREAD_PCT
        b = _make_broker(
            _FakeDataClient({"AAPL": _quote(99.75, 100.25)}),
            _FakeTradeClient(is_open=True),
        )
        result = b.check_spread("AAPL")
        assert result["ok"] is True
        assert result["spread_pct"] < 0.02

    def test_wide_spread_blocks(self):
        # bid=95, ask=105 -> spread=10/mid100=10%, over the 2% cap
        b = _make_broker(
            _FakeDataClient({"AAPL": _quote(95.0, 105.0)}),
            _FakeTradeClient(is_open=True),
        )
        result = b.check_spread("AAPL")
        assert result["ok"] is False
        assert result["reason"] == "spread_too_wide"
        assert result["spread_pct"] == pytest.approx(0.10, abs=1e-6)

    def test_missing_bid_ask_blocks(self):
        # bid=0/ask=0 -- no usable quote data
        b = _make_broker(
            _FakeDataClient({"AAPL": _quote(0, 0)}),
            _FakeTradeClient(is_open=True),
        )
        result = b.check_spread("AAPL")
        assert result["ok"] is False
        assert result["reason"] == "invalid_quote"

    def test_crossed_quote_blocks(self):
        # ask < bid -- a crossed/bad quote, must not pass through
        b = _make_broker(
            _FakeDataClient({"AAPL": _quote(101.0, 99.0)}),
            _FakeTradeClient(is_open=True),
        )
        result = b.check_spread("AAPL")
        assert result["ok"] is False
        assert result["reason"] == "invalid_quote"

    def test_market_closed_skips_check(self):
        # After-hours quotes are stale/one-sided by definition -- skip
        # (not block) rather than false-positiving on every off-hours call.
        b = _make_broker(
            _FakeDataClient({"AAPL": _quote(95.0, 105.0)}),  # would fail if checked
            _FakeTradeClient(is_open=False),
        )
        result = b.check_spread("AAPL")
        assert result["ok"] is True
        assert result["reason"] == "market_closed"

    def test_clock_check_failure_blocks(self):
        b = _make_broker(
            _FakeDataClient({"AAPL": _quote(99.75, 100.25)}),
            _FakeTradeClient(raise_on_clock=True),
        )
        result = b.check_spread("AAPL")
        assert result["ok"] is False
        assert result["reason"] == "clock_check_failed"

    def test_quote_api_timeout_blocks(self):
        # Fail-safe: an error fetching the quote must block, never pass
        # through unverified.
        b = _make_broker(_RaisingDataClient(), _FakeTradeClient(is_open=True))
        result = b.check_spread("AAPL")
        assert result["ok"] is False
        assert result["reason"] == "quote_unavailable"


class TestCheckCryptoSpread:
    def test_narrow_spread_passes(self):
        b = object.__new__(BrokerClient)

        class _FakeCryptoData:
            def get_crypto_latest_quote(self, req):
                return {"BTC/USD": _quote(49975.0, 50025.0)}

        b.crypto_data = _FakeCryptoData()
        result = b.check_crypto_spread("BTC/USD")
        assert result["ok"] is True

    def test_wide_spread_blocks(self):
        b = object.__new__(BrokerClient)

        class _FakeCryptoData:
            def get_crypto_latest_quote(self, req):
                return {"BTC/USD": _quote(47500.0, 52500.0)}  # 10% spread

        b.crypto_data = _FakeCryptoData()
        result = b.check_crypto_spread("BTC/USD")
        assert result["ok"] is False
        assert result["reason"] == "spread_too_wide"

    def test_quote_failure_blocks(self):
        b = object.__new__(BrokerClient)

        class _FakeCryptoData:
            def get_crypto_latest_quote(self, req):
                raise TimeoutError("simulated timeout")

        b.crypto_data = _FakeCryptoData()
        result = b.check_crypto_spread("BTC/USD")
        assert result["ok"] is False
        assert result["reason"] == "quote_unavailable"


class TestBuyRespectsSpreadGate:
    def test_buy_blocked_when_spread_too_wide(self, monkeypatch):
        """buy() must reject the entry before any order is submitted when
        check_spread() fails -- no order, no fill, no cost-tracking call."""
        b = object.__new__(BrokerClient)
        b.data = _FakeDataClient({"AAPL": _quote(95.0, 105.0)})
        b.trade = SimpleNamespace(
            submit_order=lambda req: pytest.fail(
                "submit_order must not be called when the spread gate blocks"
            )
        )
        monkeypatch.setattr(b, "is_market_open", lambda: True)

        result = b.buy("AAPL", dollar_amount=100.0, strategy="breakout")
        assert result["blocked"] is True
        assert result["reason"] == "spread_too_wide"
