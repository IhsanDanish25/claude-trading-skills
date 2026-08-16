"""Tests for market_close's Buffett Value exit wiring.

Buffett Value carries no stop-loss and no calendar exit by design (see
skills/buffett-value/scripts/sell.py) -- these tests cover: (1) the 14:45
late-trail ratchet skips Buffett Value positions rather than tightening a
stop that was never attached, and (2) _run_buffett_value_exits evaluates
and executes exits via the hold/sell agents, independent of the generic
per-position review loop.

Pure-logic tests: module-level buffett_* names monkeypatched directly,
mirroring test_market_close_guards.py's plain-import pattern.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

import pytz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_close

ET = pytz.timezone("America/New_York")


def _position(symbol, qty=10.0, avg_entry_price=100.0):
    return SimpleNamespace(symbol=symbol, qty=qty, avg_entry_price=avg_entry_price)


class TestLateTrailSkipsBuffettValue:
    def test_buffett_value_position_never_gets_a_tightened_stop(self, monkeypatch):
        now = datetime.datetime.now(ET).replace(hour=14, minute=50)
        monkeypatch.setattr(market_close.datetime, "datetime",
                             SimpleNamespace(now=lambda tz=None: now))
        monkeypatch.setattr(market_close, "buffett_all", lambda: {"CMCSA": {"strategy": "buffett_value"}})
        monkeypatch.setattr(market_close, "is_base_symbol", lambda sym: False)
        monkeypatch.setattr(market_close, "get_quotes", lambda syms: {"CMCSA": {"price": 110.0}})

        broker = SimpleNamespace(
            get_positions=lambda: [_position("CMCSA", avg_entry_price=100.0)],
            get_open_orders=lambda: [],
        )

        def _exploding_tighten(*a, **k):
            raise AssertionError("tighten_stop must never be called on a Buffett Value position")

        broker.tighten_stop = _exploding_tighten

        tightened = market_close._late_trail(broker)

        assert tightened == 0


class TestRunBuffettValueExits:
    def _snapshot(self):
        return {
            "symbol": "CMCSA",
            "conviction_score": 0.6,
            "valuation": {"valuation_details": {"graham_number": 42.15}},
        }

    def test_no_tracked_positions_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(market_close, "buffett_all", lambda: {})
        broker = SimpleNamespace()

        exited = market_close._run_buffett_value_exits(broker)

        assert exited == 0

    def _clean_monitor_portfolio(self, positions_list, candidate_pool=None):
        return [
            {"symbol": p["symbol"], "fundamentals_deteriorated": False,
             "deterioration_reasons": [], "better_opportunity": None}
            for p in positions_list
        ]

    def test_missing_entry_price_skips_evaluation_without_crashing(self, monkeypatch):
        """Regression: a corrupted/hand-edited state record with no
        entry_price used to reach sell.py's evaluate_exit(), which computes
        current_price - entry_price -- raising TypeError against None,
        silently swallowed by the broad except. Worse, if left to a naive
        `or 0.0` fallback, profit_per_share would collapse to current_price,
        which can look like a spurious profit-target hit. Must skip cleanly
        with a clear log instead of either failure mode."""
        monkeypatch.setattr(market_close, "buffett_all", lambda: {
            "CMCSA": {"qty": 10, "entry_snapshot": self._snapshot()}  # no entry_price key
        })
        monitor_called = []
        monkeypatch.setattr(market_close, "buffett_monitor_portfolio",
                             lambda positions_list, candidate_pool=None: monitor_called.append(positions_list))

        def _exploding_evaluate_exit(*a, **k):
            raise AssertionError("evaluate_exit must not be reached with a missing entry_price")

        monkeypatch.setattr(market_close, "buffett_evaluate_exit", _exploding_evaluate_exit)

        broker = SimpleNamespace()  # no methods needed -- must never be touched

        exited = market_close._run_buffett_value_exits(broker)

        assert exited == 0
        assert monitor_called == []  # skipped before even re-screening fundamentals

    def test_monitor_portfolio_batch_failure_skips_run_without_crashing(self, monkeypatch):
        """Regression: monitor_portfolio() is now one batch call across all
        tracked positions (needed to share a single candidate_pool for the
        weekly better-opportunity check), not a per-symbol call inside each
        symbol's own try/except like before. A batch-level failure must not
        take down the whole routine or silently corrupt state -- skip this
        run's exits and retry next time."""
        monkeypatch.setattr(market_close, "buffett_all", lambda: {
            "CMCSA": {"entry_price": 26.18, "qty": 10, "entry_snapshot": self._snapshot()}
        })

        def _exploding_monitor(positions_list, candidate_pool=None):
            raise RuntimeError("hold.py blew up")

        monkeypatch.setattr(market_close, "buffett_monitor_portfolio", _exploding_monitor)

        def _exploding_evaluate_exit(*a, **k):
            raise AssertionError("evaluate_exit must not be reached after a batch failure")

        monkeypatch.setattr(market_close, "buffett_evaluate_exit", _exploding_evaluate_exit)

        broker = SimpleNamespace()  # must never be touched

        exited = market_close._run_buffett_value_exits(broker)

        assert exited == 0

    def test_hold_when_no_exit_condition_met(self, monkeypatch):
        monkeypatch.setattr(market_close, "buffett_all", lambda: {
            "CMCSA": {"entry_price": 26.18, "qty": 10, "entry_snapshot": self._snapshot()}
        })
        monkeypatch.setattr(market_close, "buffett_monitor_portfolio", self._clean_monitor_portfolio)
        monkeypatch.setattr(market_close, "buffett_evaluate_exit", lambda pos, mon, price: None)

        untracked = []
        monkeypatch.setattr(market_close, "buffett_untrack", untracked.append)

        broker = SimpleNamespace(
            get_position=lambda sym: _position(sym, qty=10.0),
            get_price=lambda sym: 30.0,
        )

        def _exploding_sell(*a, **k):
            raise AssertionError("sell_limit must not be called when HOLD")

        broker.sell_limit = _exploding_sell

        exited = market_close._run_buffett_value_exits(broker)

        assert exited == 0
        assert untracked == []

    def test_sells_and_untracks_on_exit_decision(self, monkeypatch):
        monkeypatch.setattr(market_close, "buffett_all", lambda: {
            "CMCSA": {"entry_price": 26.18, "qty": 10, "entry_snapshot": self._snapshot()}
        })
        monkeypatch.setattr(market_close, "buffett_monitor_portfolio", self._clean_monitor_portfolio)

        exit_decision = {
            "symbol": "CMCSA",
            "qty": 10,
            "reason_code": "profit_target",
            "reason_detail": "Price $42.57 reached fair value target $42.15",
            "order": {"symbol": "CMCSA", "qty": 10, "limit_price": 42.53},
        }
        monkeypatch.setattr(market_close, "buffett_evaluate_exit", lambda pos, mon, price: exit_decision)

        sold = []
        untracked = []
        monkeypatch.setattr(market_close, "buffett_untrack", untracked.append)
        monkeypatch.setattr(market_close, "send_trade_alert", lambda **k: True)

        broker = SimpleNamespace(
            get_position=lambda sym: _position(sym, qty=10.0),
            get_price=lambda sym: 42.57,
        )

        def _sell_limit(symbol, qty, limit_price):
            sold.append((symbol, qty, limit_price))
            return {"order": None, "qty": qty, "limit_price": limit_price,
                    "filled": True, "filled_qty": qty, "filled_avg_price": limit_price}

        broker.sell_limit = _sell_limit

        exited = market_close._run_buffett_value_exits(broker)

        assert exited == 1
        assert sold == [("CMCSA", 10, 42.53)]
        assert untracked == ["CMCSA"]

    def test_does_not_untrack_or_alert_when_sell_limit_not_confirmed_filled(self, monkeypatch):
        """Regression: sell_limit() doesn't guarantee an immediate fill (it's a
        resting DAY limit order). Before this fix, market_close logged
        success, sent a trade alert claiming the sale executed, and untracked
        the position right after submitting -- even if the order was still
        resting or had already expired unfilled. A false 'sold' record left
        a real open position invisible to every tracked_buffett exclusion check."""
        monkeypatch.setattr(market_close, "buffett_all", lambda: {
            "CMCSA": {"entry_price": 26.18, "qty": 10, "entry_snapshot": self._snapshot()}
        })
        monkeypatch.setattr(market_close, "buffett_monitor_portfolio", self._clean_monitor_portfolio)
        exit_decision = {
            "symbol": "CMCSA",
            "qty": 10,
            "reason_code": "profit_target",
            "reason_detail": "Price $42.57 reached fair value target $42.15",
            "order": {"symbol": "CMCSA", "qty": 10, "limit_price": 42.53},
        }
        monkeypatch.setattr(market_close, "buffett_evaluate_exit", lambda pos, mon, price: exit_decision)

        untracked = []
        alerted = []
        monkeypatch.setattr(market_close, "buffett_untrack", untracked.append)
        monkeypatch.setattr(market_close, "send_trade_alert", lambda **k: alerted.append(k))

        broker = SimpleNamespace(
            get_position=lambda sym: _position(sym, qty=10.0),
            get_price=lambda sym: 42.57,
            sell_limit=lambda symbol, qty, limit_price: {
                "order": None, "qty": qty, "limit_price": limit_price,
                "filled": False, "filled_qty": None, "filled_avg_price": None,
            },
        )

        exited = market_close._run_buffett_value_exits(broker)

        assert exited == 0
        assert untracked == []
        assert alerted == []

    def test_untracks_when_alpaca_position_no_longer_exists(self, monkeypatch):
        monkeypatch.setattr(market_close, "buffett_all", lambda: {
            "CMCSA": {"entry_price": 26.18, "qty": 10, "entry_snapshot": self._snapshot()}
        })
        monkeypatch.setattr(market_close, "buffett_monitor_portfolio", self._clean_monitor_portfolio)

        untracked = []
        monkeypatch.setattr(market_close, "buffett_untrack", untracked.append)

        broker = SimpleNamespace(get_position=lambda sym: None)

        exited = market_close._run_buffett_value_exits(broker)

        assert exited == 0
        assert untracked == ["CMCSA"]

    def test_one_symbol_failing_does_not_block_others(self, monkeypatch):
        """Per-symbol isolation now applies to the post-monitor part of the
        loop (position lookup, evaluate_exit, sell_limit) rather than the
        monitor call itself, which is a single batch call across all
        positions (see test_monitor_portfolio_batch_failure_skips_run...).
        A failure fetching one symbol's live Alpaca position must still not
        block evaluation of the others in the same run."""
        monkeypatch.setattr(market_close, "buffett_all", lambda: {
            "CMCSA": {"entry_price": 26.18, "qty": 10, "entry_snapshot": self._snapshot()},
            "AAPL": {"entry_price": 200.0, "qty": 5, "entry_snapshot": self._snapshot()},
        })
        monkeypatch.setattr(market_close, "buffett_monitor_portfolio", self._clean_monitor_portfolio)
        monkeypatch.setattr(market_close, "buffett_evaluate_exit", lambda pos, mon, price: None)

        def _get_position(sym):
            if sym == "CMCSA":
                raise RuntimeError("Alpaca API boom")
            return _position(sym, qty=5.0)

        broker = SimpleNamespace(
            get_position=_get_position,
            get_price=lambda sym: 210.0,
        )

        exited = market_close._run_buffett_value_exits(broker)

        assert exited == 0  # AAPL held, CMCSA errored but didn't crash the loop
