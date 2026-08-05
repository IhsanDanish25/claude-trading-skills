"""Regression tests for the "flatten on stop-loss attach failure" alerting.

2026-08-05 12:07 ET incident: midday_review flattened WFC (and BAC, NKE) after
Alpaca verification found no live stop/OCO order, but send_trade_alert() was
called without the required `stop`/`target` positional args and raised
(missing 2 required positional arguments), so the flatten happened silently
with no alert. A near-identical flatten path in market_open._run_pead had the
same bug, and midday_review's midday-scan-buy flatten never called
send_trade_alert() at all. All three are fixed to always alert on flatten.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import midday_review
import market_open
from core import config


def _position(symbol, avg_entry_price, qty):
    p = MagicMock()
    p.symbol = symbol
    p.avg_entry_price = avg_entry_price
    p.qty = qty
    p.unrealized_pl = 0
    return p


class TestMiddayReviewFlattenAlert:
    """Regression for routines/midday_review.py's per-position protection loop."""

    def _run_with_failed_attach(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
        monkeypatch.setattr(midday_review.config, "validate", lambda: None)
        monkeypatch.setattr(midday_review, "_load_pyramided", lambda: set())
        monkeypatch.setattr(midday_review, "_save_pyramided", lambda symbols: None)
        monkeypatch.setattr(midday_review.time, "sleep", lambda *_: None)
        monkeypatch.setattr(midday_review, "is_base_symbol", lambda sym: False)
        monkeypatch.setattr(midday_review, "get_market_breadth", lambda: {})
        monkeypatch.setattr(midday_review, "analyze_market_regime",
                             lambda breadth: {"regime": "neutral", "trade_bias": "defensive"})
        monkeypatch.setattr(midday_review, "get_quotes", lambda symbols: {})
        monkeypatch.setattr(midday_review, "review_open_positions", lambda pos_data, regime: [])
        monkeypatch.setattr(midday_review, "spy_log", lambda broker: None)
        monkeypatch.setattr(midday_review, "rebalance_to_spy", lambda broker: {"action": "none"})
        monkeypatch.setattr(midday_review, "cancel_open_sell_orders", lambda broker, sym: None)

        alert = MagicMock()
        monkeypatch.setattr(midday_review, "send_trade_alert", alert)

        broker = MagicMock()
        broker.is_market_open.return_value = True
        broker.portfolio_value.return_value = 269.48
        broker.position_count.return_value = 1
        broker.get_account.return_value = MagicMock(equity=269.48)
        broker.get_positions.return_value = [_position("WFC", 87.32, 1)]
        broker.get_open_orders.return_value = []
        broker.get_price.return_value = 89.12
        broker.attach_stop_target.return_value = (False, None)  # verified-failed, like the incident

        monkeypatch.setattr(midday_review, "BrokerClient", lambda: broker)

        midday_review.run()
        return broker, alert

    def test_flatten_on_verified_attach_failure_passes_stop_and_target(self, monkeypatch, tmp_path):
        broker, alert = self._run_with_failed_attach(monkeypatch, tmp_path)

        broker.sell.assert_called_once_with("WFC", qty=1)
        alert.assert_called_once()
        kwargs = alert.call_args.kwargs
        assert kwargs["action"] == "FLATTEN"
        assert kwargs["ticker"] == "WFC"
        assert kwargs["shares"] == 1
        # The bug: these were previously omitted entirely, and
        # send_trade_alert() requires them (TypeError swallowed by the
        # surrounding except-block, so no alert ever went out).
        assert "stop" in kwargs
        assert "target" in kwargs
        assert kwargs["stop"] > 0
        assert kwargs["target"] > 0

    def test_flatten_alert_is_never_raised_by_a_real_signature_mismatch(self, monkeypatch, tmp_path):
        """Use the real send_trade_alert (not a mock) so a missing required
        arg would raise TypeError, exactly like the incident's silent
        failure. No network mocking needed: send_trade_alert() silently
        no-ops when RESEND_API_KEY isn't set (core/notifier.py:send), so this
        stays a pure signature/argument-completeness test."""
        import core.notifier as notifier_module
        monkeypatch.setattr(notifier_module, "_API_KEY", "")
        monkeypatch.setattr(midday_review.config, "STATE_DIR", str(tmp_path))
        monkeypatch.setattr(midday_review.config, "validate", lambda: None)
        monkeypatch.setattr(midday_review, "_load_pyramided", lambda: set())
        monkeypatch.setattr(midday_review, "_save_pyramided", lambda symbols: None)
        monkeypatch.setattr(midday_review.time, "sleep", lambda *_: None)
        monkeypatch.setattr(midday_review, "is_base_symbol", lambda sym: False)
        monkeypatch.setattr(midday_review, "get_market_breadth", lambda: {})
        monkeypatch.setattr(midday_review, "analyze_market_regime",
                             lambda breadth: {"regime": "neutral", "trade_bias": "defensive"})
        monkeypatch.setattr(midday_review, "get_quotes", lambda symbols: {})
        monkeypatch.setattr(midday_review, "review_open_positions", lambda pos_data, regime: [])
        monkeypatch.setattr(midday_review, "spy_log", lambda broker: None)
        monkeypatch.setattr(midday_review, "rebalance_to_spy", lambda broker: {"action": "none"})
        monkeypatch.setattr(midday_review, "cancel_open_sell_orders", lambda broker, sym: None)

        broker = MagicMock()
        broker.is_market_open.return_value = True
        broker.portfolio_value.return_value = 269.48
        broker.position_count.return_value = 1
        broker.get_account.return_value = MagicMock(equity=269.48)
        broker.get_positions.return_value = [_position("WFC", 87.32, 1)]
        broker.get_open_orders.return_value = []
        broker.get_price.return_value = 89.12
        broker.attach_stop_target.return_value = (False, None)

        monkeypatch.setattr(midday_review, "BrokerClient", lambda: broker)

        # Must not raise TypeError for missing stop/target, and the position
        # must actually get sold (flattened).
        midday_review.run()
        broker.sell.assert_called_once_with("WFC", qty=1)


class TestMiddayScanBuyFlattenAlert:
    """Regression: the midday-scan buy path flattened on failed stop-attach
    but never called send_trade_alert() at all (silent, not even broken)."""

    def test_midday_scan_flatten_now_alerts(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
        monkeypatch.setattr(midday_review.config, "validate", lambda: None)
        monkeypatch.setattr(midday_review, "_load_pyramided", lambda: set())
        monkeypatch.setattr(midday_review, "_save_pyramided", lambda symbols: None)
        monkeypatch.setattr(midday_review.time, "sleep", lambda *_: None)
        monkeypatch.setattr(midday_review, "is_base_symbol", lambda sym: False)
        monkeypatch.setattr(midday_review, "get_market_breadth", lambda: {})
        monkeypatch.setattr(midday_review, "analyze_market_regime",
                             lambda breadth: {"regime": "neutral", "trade_bias": "moderate"})
        monkeypatch.setattr(midday_review, "get_quotes", lambda symbols: {})
        monkeypatch.setattr(midday_review, "review_open_positions", lambda pos_data, regime: [])
        monkeypatch.setattr(midday_review, "spy_log", lambda broker: None)
        monkeypatch.setattr(midday_review, "rebalance_to_spy", lambda broker: {"action": "none"})
        monkeypatch.setattr(midday_review, "screen",
                             lambda: [{"symbol": "PANW", "price": 200.0}])
        monkeypatch.setattr(midday_review, "score_vcp_candidates",
                             lambda top: [{"symbol": "PANW", "score": 90, "reason": "tight VCP",
                                           "action": "BUY"}])
        monkeypatch.setattr(midday_review, "_load_today_bought", lambda: set())
        monkeypatch.setattr(midday_review, "_mark_bought", lambda sym: None)

        alert = MagicMock()
        monkeypatch.setattr(midday_review, "send_trade_alert", alert)

        broker = MagicMock()
        broker.is_market_open.return_value = True
        broker.portfolio_value.return_value = 100_000.0
        broker.position_count.return_value = 0
        broker.get_account.return_value = MagicMock(equity=100_000.0)
        broker.get_positions.return_value = []
        broker.get_open_orders.return_value = []
        broker.affordable_budget.return_value = 100_000.0
        broker.buy.return_value = {
            "qty": 10, "price": 200.0, "stop": 184.0, "target": 220.0,
            "stop_attached": False,
        }

        monkeypatch.setattr(midday_review, "BrokerClient", lambda: broker)

        midday_review.run()

        broker.sell.assert_called_once_with("PANW", qty=10)
        alert.assert_called_once()
        kwargs = alert.call_args.kwargs
        assert kwargs["action"] == "FLATTEN"
        assert kwargs["ticker"] == "PANW"
        assert kwargs["stop"] == 184.0
        assert kwargs["target"] == 220.0


class TestMarketOpenPeadFlattenAlert:
    """Regression for routines/market_open.py's _run_pead flatten-on-attach-failure."""

    def test_pead_flatten_passes_stop_and_target(self, monkeypatch):
        monkeypatch.setattr(market_open, "_sector_gate", lambda *a, **k: True)
        monkeypatch.setattr(market_open, "free_cash_for_pead", lambda broker, amount: True)
        monkeypatch.setattr(market_open, "spy_log", lambda broker: None)
        monkeypatch.setattr(market_open, "rebalance_to_spy", lambda broker: {"action": "none"})
        monkeypatch.setattr(market_open.trade_logger, "log_event", lambda *a, **k: None)

        alert = MagicMock()
        monkeypatch.setattr(market_open, "send_trade_alert", alert)

        broker = MagicMock()
        broker.buy.return_value = {
            "qty": 5, "price": 50.0, "stop": 42.5, "target": None,
            "stop_attached": False,
        }
        cb = MagicMock()
        candidates = [{
            "symbol": "ACME", "surprise_pct": 12.0, "actual_eps": 1.1,
            "estimated_eps": 0.9, "report_date": "2026-08-04", "price": 50.0,
        }]
        monkeypatch.setattr(market_open, "screen_earnings", lambda **k: candidates)
        monkeypatch.setattr(market_open, "_affordable_candidates",
                             lambda broker, cands, strategy, log: cands)

        market_open._run_pead(
            broker=broker, cb=cb, pv=100_000.0, slots=[5],
            held=set(), already_bought_today=set(), sector_counts={},
        )

        broker.sell.assert_called_once_with("ACME", qty=5)
        alert.assert_called_once()
        kwargs = alert.call_args.kwargs
        assert kwargs["action"] == "FLATTEN"
        assert kwargs["ticker"] == "ACME"
        assert kwargs["shares"] == 5
        assert kwargs["price"] == 50.0
        assert kwargs["stop"] == 42.5
        assert kwargs["target"] is None  # PEAD has no hard target (time-managed exit)
