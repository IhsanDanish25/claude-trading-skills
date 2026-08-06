"""Tests for market_close order matching and SPY base exclusion."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpaca.trading.enums import OrderSide, OrderType

import market_close


def _order(symbol, otype, side=OrderSide.SELL, stop_price=None):
    return SimpleNamespace(symbol=symbol, type=otype, side=side,
                           stop_price=stop_price)


class TestBuildStopMap:
    def test_maps_real_enum_stop_orders(self):
        """Regression: str(enum) comparisons meant stop_map was always empty,
        so the 14:45 late-trail ratchet never saw existing stops."""
        orders = [
            _order("AAPL", OrderType.STOP, stop_price=95.0),
            _order("MSFT", OrderType.LIMIT, stop_price=None),
            _order("NVDA", OrderType.STOP, side=OrderSide.BUY, stop_price=88.0),
        ]
        assert market_close._build_stop_map(orders) == {"AAPL": 95.0}

    def test_maps_stop_limit_orders(self):
        """Regression: attach_stop_target always sets a limit_price alongside
        stop_price, so Alpaca classifies the resulting order as STOP_LIMIT,
        not STOP — an exact "== stop" match left the map empty for every
        real protective order actually placed by the broker."""
        orders = [_order("BAC", OrderType.STOP_LIMIT, stop_price=57.74)]
        assert market_close._build_stop_map(orders) == {"BAC": 57.74}

    def test_ignores_orders_without_numeric_stop(self):
        orders = [_order("AAPL", OrderType.STOP, stop_price=None)]
        assert market_close._build_stop_map(orders) == {}


class TestResolveEodStop:
    """_resolve_eod_stop decides the stop re-attached after market_close's
    cancel-all-orders step. Regression coverage for the 21hr unprotected
    window: a HOLD/TIGHTEN_STOP decision used to leave positions with no
    stop at all from 3pm ET until midday_review the next day."""

    def test_hold_uses_base_stop(self):
        assert market_close._resolve_eod_stop(9.19, "HOLD", None) == 9.19

    def test_tighten_stop_with_better_price_wins(self):
        assert market_close._resolve_eod_stop(9.19, "TIGHTEN_STOP", 9.50) == 9.50

    def test_tighten_stop_with_looser_price_falls_back(self):
        """A new_stop below the entry-based default would weaken protection
        — ignore it and keep the safer base stop."""
        assert market_close._resolve_eod_stop(9.19, "TIGHTEN_STOP", 8.90) == 9.19

    def test_tighten_stop_missing_new_stop_falls_back(self):
        assert market_close._resolve_eod_stop(9.19, "TIGHTEN_STOP", None) == 9.19

    def test_tighten_stop_non_numeric_new_stop_falls_back(self):
        assert market_close._resolve_eod_stop(9.19, "TIGHTEN_STOP", "not-a-number") == 9.19


class TestTodaysTradesAndSkippedRoutines:
    """EOD summary helpers reading state/trade_log.jsonl and
    state/skipped_routines.json — feeds the daily summary email so trades
    and silently-skipped routines don't require reading Railway logs."""

    def test_todays_trades_filters_by_date(self, monkeypatch, tmp_path):
        log_path = tmp_path / "trade_log.jsonl"
        log_path.write_text(
            '{"ts": "2026-08-05T09:44:33", "symbol": "BRTMU", "side": "buy", "qty": 1, "price": 9.99, "strategy": "insider"}\n'
            '{"ts": "2026-08-04T10:00:00", "symbol": "WFC", "side": "buy", "qty": 1, "price": 87.32, "strategy": "earnmom"}\n'
        )
        monkeypatch.setattr(market_close, "TRADE_LOG_PATH", str(log_path))

        trades = market_close._todays_trades("2026-08-05")
        assert len(trades) == 1
        assert trades[0]["symbol"] == "BRTMU"

    def test_todays_trades_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(market_close, "TRADE_LOG_PATH", str(tmp_path / "nope.jsonl"))
        assert market_close._todays_trades("2026-08-05") == []

    def test_todays_skipped_routines_matches_date(self, monkeypatch, tmp_path):
        import json
        skip_path = tmp_path / "skipped_routines.json"
        skip_path.write_text(json.dumps({
            "date": "2026-08-05",
            "skipped": [{"module": "routines.market_open", "reason": "7.5h late", "at": "17:15:08"}],
        }))
        monkeypatch.setattr(market_close, "SKIPPED_ROUTINES_FILE", str(skip_path))

        skipped = market_close._todays_skipped_routines("2026-08-05")
        assert len(skipped) == 1
        assert skipped[0]["module"] == "routines.market_open"

    def test_todays_skipped_routines_wrong_date_returns_empty(self, monkeypatch, tmp_path):
        import json
        skip_path = tmp_path / "skipped_routines.json"
        skip_path.write_text(json.dumps({
            "date": "2026-08-04",
            "skipped": [{"module": "routines.market_open", "reason": "stale", "at": "17:15:08"}],
        }))
        monkeypatch.setattr(market_close, "SKIPPED_ROUTINES_FILE", str(skip_path))

        assert market_close._todays_skipped_routines("2026-08-05") == []
