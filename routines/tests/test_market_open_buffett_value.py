"""Tests for market_open's Buffett Value entry handler (_run_buffett_value).

Covers the buffett_value-specific behavioral guarantees: conviction-based
position sizing (not a static *_SIZE_PCT config value like other
strategies), the entry snapshot passed to buffett_track for later exit
comparison, and that buy_simple() (no stop) is used, never buy() -- plus
the standard held/already-bought-today/blocked gating every strategy
handler shares.

Pure-logic tests: module-level names monkeypatched directly, mirroring
test_market_close_buffett_value.py's pattern.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_open


def _candidate(symbol="CMCSA", conviction_score=0.75):
    return {
        "symbol": symbol,
        "conviction_score": conviction_score,
        "company_name": "Comcast Corp",
        "valuation": {"valuation_details": {"graham_number": 42.15}},
    }


def _buy_signal(symbol="CMCSA", conviction_score=0.75, position_size_pct=0.06,
                 signal_price=26.18, patterns=None):
    return {
        "symbol": symbol,
        "conviction_score": conviction_score,
        "position_size_pct": position_size_pct,
        "signal_price": signal_price,
        "patterns_detected": patterns or ["hammer"],
        "entry_reason": "Bullish hammer pattern",
    }


def _passthrough_gates(monkeypatch):
    """Neutralize the shared cross-strategy gates so tests only exercise
    buffett_value-specific behavior."""
    monkeypatch.setattr(market_open, "_sector_gate", lambda *a, **k: True)
    monkeypatch.setattr(market_open, "free_cash_for_pead", lambda broker, amount: True)
    monkeypatch.setattr(market_open.trade_logger, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(market_open, "send_trade_alert", lambda **k: True)
    monkeypatch.setattr(market_open, "_mark_bought", lambda sym, result: None)
    monkeypatch.setattr(market_open, "_append_trade_log", lambda entry: None)


def _cb():
    return SimpleNamespace(check_before_order=lambda intended_notional, symbol: None)


def test_no_candidates_takes_no_action(monkeypatch):
    monkeypatch.setattr(market_open, "screen_for_buffett_candidates", lambda universe: [])

    def _exploding_buy_signals(*a, **k):
        raise AssertionError("get_top_buy_signals must not run with zero candidates")

    monkeypatch.setattr(market_open, "get_top_buy_signals", _exploding_buy_signals)

    broker = SimpleNamespace()
    slots = [3]
    market_open._run_buffett_value(broker, _cb(), 100_000.0, slots, set(), set(), {})

    assert slots == [3]


def test_candidates_but_no_buy_signal_takes_no_action(monkeypatch):
    monkeypatch.setattr(market_open, "screen_for_buffett_candidates", lambda universe: [_candidate()])
    monkeypatch.setattr(market_open, "get_top_buy_signals", lambda candidates, limit: [])

    broker = SimpleNamespace()

    def _exploding_buy_simple(*a, **k):
        raise AssertionError("buy_simple must not be called with no buy signal")

    broker.buy_simple = _exploding_buy_simple

    slots = [3]
    market_open._run_buffett_value(broker, _cb(), 100_000.0, slots, set(), set(), {})

    assert slots == [3]


def test_happy_path_sizes_by_conviction_not_static_pct(monkeypatch):
    """Regression: every other strategy sizes off a static config.*_SIZE_PCT;
    Buffett Value sizes off the buy agent's per-candidate conviction score
    (buy.calculate_position_size, 3-10% range) instead."""
    _passthrough_gates(monkeypatch)
    monkeypatch.setattr(market_open, "screen_for_buffett_candidates",
                         lambda universe: [_candidate(conviction_score=0.75)])
    monkeypatch.setattr(market_open, "get_top_buy_signals",
                         lambda candidates, limit: [_buy_signal(position_size_pct=0.08)])

    tracked = []
    monkeypatch.setattr(market_open, "buffett_track",
                         lambda sym, price, qty, entry_snapshot: tracked.append(
                             (sym, price, qty, entry_snapshot)))

    broker = SimpleNamespace()
    buy_calls = []

    def _buy_simple(symbol, dollar_amount, strategy=None):
        buy_calls.append((symbol, dollar_amount, strategy))
        return {"qty": 30, "price": 26.20}

    broker.buy_simple = _buy_simple

    pv = 100_000.0
    slots = [3]
    market_open._run_buffett_value(broker, _cb(), pv, slots, set(), set(), {})

    assert len(buy_calls) == 1
    symbol, dollar_amount, strategy = buy_calls[0]
    assert symbol == "CMCSA"
    assert dollar_amount == pv * 0.08  # conviction-based, not a static SIZE_PCT
    assert strategy == "buffett_value"
    assert slots == [2]

    assert len(tracked) == 1
    tracked_sym, tracked_price, tracked_qty, tracked_snapshot = tracked[0]
    assert tracked_sym == "CMCSA"
    assert tracked_price == 26.20
    assert tracked_qty == 30
    assert tracked_snapshot["symbol"] == "CMCSA"  # the full analyst candidate, for hold.py later


def test_buy_simple_never_receives_a_stop_loss_pct(monkeypatch):
    """Regression: every other strategy calls broker.buy() with
    stop_loss_pct/take_profit_pct; Buffett Value must go through
    buy_simple() only, which has no such parameter."""
    _passthrough_gates(monkeypatch)
    monkeypatch.setattr(market_open, "screen_for_buffett_candidates", lambda universe: [_candidate()])
    monkeypatch.setattr(market_open, "get_top_buy_signals", lambda candidates, limit: [_buy_signal()])
    monkeypatch.setattr(market_open, "buffett_track", lambda *a, **k: None)

    broker = SimpleNamespace()
    received_kwargs = {}

    def _buy_simple(symbol, dollar_amount, strategy=None):
        received_kwargs.update({"symbol": symbol, "dollar_amount": dollar_amount, "strategy": strategy})
        return {"qty": 10, "price": 26.20}

    broker.buy_simple = _buy_simple

    def _exploding_buy(*a, **k):
        raise AssertionError("broker.buy() (stop-coupled) must never be called by buffett_value")

    broker.buy = _exploding_buy

    market_open._run_buffett_value(broker, _cb(), 100_000.0, [3], set(), set(), {})

    assert "stop_loss_pct" not in received_kwargs
    assert "take_profit_pct" not in received_kwargs


def test_skips_symbol_already_held(monkeypatch):
    _passthrough_gates(monkeypatch)
    monkeypatch.setattr(market_open, "screen_for_buffett_candidates", lambda universe: [_candidate()])
    monkeypatch.setattr(market_open, "get_top_buy_signals", lambda candidates, limit: [_buy_signal()])

    broker = SimpleNamespace()

    def _exploding_buy_simple(*a, **k):
        raise AssertionError("buy_simple must not be called for an already-held symbol")

    broker.buy_simple = _exploding_buy_simple

    slots = [3]
    market_open._run_buffett_value(broker, _cb(), 100_000.0, slots, {"CMCSA"}, set(), {})

    assert slots == [3]


def test_skips_symbol_already_bought_today(monkeypatch):
    _passthrough_gates(monkeypatch)
    monkeypatch.setattr(market_open, "screen_for_buffett_candidates", lambda universe: [_candidate()])
    monkeypatch.setattr(market_open, "get_top_buy_signals", lambda candidates, limit: [_buy_signal()])

    broker = SimpleNamespace()

    def _exploding_buy_simple(*a, **k):
        raise AssertionError("buy_simple must not be called for a symbol already bought today")

    broker.buy_simple = _exploding_buy_simple

    slots = [3]
    market_open._run_buffett_value(broker, _cb(), 100_000.0, slots, set(), {"CMCSA"}, {})

    assert slots == [3]


def test_blocked_buy_does_not_track_or_consume_a_slot(monkeypatch):
    _passthrough_gates(monkeypatch)
    monkeypatch.setattr(market_open, "screen_for_buffett_candidates", lambda universe: [_candidate()])
    monkeypatch.setattr(market_open, "get_top_buy_signals", lambda candidates, limit: [_buy_signal()])

    tracked = []
    monkeypatch.setattr(market_open, "buffett_track", lambda *a, **k: tracked.append(a))

    broker = SimpleNamespace(buy_simple=lambda symbol, dollar_amount, strategy=None: {
        "blocked": True, "reason": "spread_too_wide"
    })

    slots = [3]
    market_open._run_buffett_value(broker, _cb(), 100_000.0, slots, set(), set(), {})

    assert slots == [3]
    assert tracked == []
