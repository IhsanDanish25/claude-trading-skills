"""Regression: auto_trader._place_trade's fractional/notional buy path must
attach BOTH a protective stop-loss and a take-profit order at the broker,
not take-profit alone.

Prior behavior: the `if use_notional:` branch skipped the stop entirely,
with a comment claiming "Alpaca doesn't support GTC stops on fractional".
That's true for GTC specifically, but Alpaca does support standalone
stop/stop-limit orders on fractional-qty positions with time_in_force=DAY
(see core/broker.py's attach_stop_target, which already relies on this via
`tif = TimeInForce.DAY if qty != int(qty) else TimeInForce.GTC`). A
fractional position left with no stop at all is a silent risk-management
gap — this test locks in that both legs get submitted, as two independent
standalone orders (not bracket/OCO, which is unreliable combined with
fractional qty per Alpaca's own community reports).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopOrderRequest

import auto_trader


def _mock_client(*, cash: float, filled_qty: str):
    client = MagicMock()
    client.get_account.return_value = MagicMock(cash=cash)
    client.submit_order.return_value = MagicMock(id="order-abc123")
    client.get_open_position.return_value = MagicMock(qty=filled_qty)
    return client


def test_fractional_buy_attaches_both_stop_and_take_profit(monkeypatch):
    monkeypatch.setattr(auto_trader, "DRY_RUN", False)
    # wait_for_fill isn't exercised by the fractional branch's own logic
    # (it derives filled_qty_f from the live position instead) — stub it
    # out so the test doesn't poll/sleep.
    monkeypatch.setattr(auto_trader, "wait_for_fill", lambda client, order_id: 0)

    # cash small enough / price high enough that shares resolves to 0,
    # forcing the use_notional branch (mirrors core/broker.py's fractional
    # fallback test fixtures: high price, small account).
    price = 339.0
    client = _mock_client(cash=50.0, filled_qty="0.036873")

    filled, new_today_bought = auto_trader._place_trade(
        client, None, "XYZ", price, 0.05, 0.10,
        {"confidence": 8}, None, [], {}, {}, None, set(),
    )

    assert filled == 1
    assert "XYZ" in new_today_bought

    submitted = [c.args[0] for c in client.submit_order.call_args_list]
    stop_orders = [r for r in submitted if isinstance(r, StopOrderRequest)]
    tp_orders = [r for r in submitted if isinstance(r, LimitOrderRequest)]
    buy_orders = [r for r in submitted if isinstance(r, MarketOrderRequest)]

    # The fix: a live stop order AND a take-profit order, not TP alone.
    assert len(buy_orders) == 1
    assert len(stop_orders) == 1, "fractional buy must attach a protective stop"
    assert len(tp_orders) == 1

    stop_req = stop_orders[0]
    assert stop_req.symbol == "XYZ"
    assert float(stop_req.qty) == 0.036873
    assert stop_req.stop_price == round(price * 0.95, 2)
    # Fractional qty rejects GTC (Alpaca error 42210000) — must be DAY.
    assert stop_req.time_in_force == TimeInForce.DAY
    # Standalone order, not part of a bracket/OCO — StopOrderRequest here
    # carries no order_class/take_profit/stop_loss linkage.
    assert getattr(stop_req, "order_class", None) in (None, "simple")

    tp_req = tp_orders[0]
    assert tp_req.symbol == "XYZ"
    assert float(tp_req.qty) == 0.036873
    assert tp_req.limit_price == round(price * 1.10, 2)


def test_whole_share_buy_still_uses_trailing_stop_not_standalone(monkeypatch):
    """Sanity check the fix is scoped to the fractional branch only — the
    whole-share path keeps using attach_trailing_stop(), unchanged."""
    monkeypatch.setattr(auto_trader, "DRY_RUN", False)
    monkeypatch.setattr(auto_trader, "wait_for_fill", lambda client, order_id: 10)

    price = 50.0
    client = _mock_client(cash=100_000.0, filled_qty="10")

    filled, new_today_bought = auto_trader._place_trade(
        client, None, "AAPL", price, 0.05, 0.10,
        {"confidence": 8}, None, [], {}, {}, None, set(),
    )

    assert filled == 1
    submitted = [c.args[0] for c in client.submit_order.call_args_list]
    assert not any(isinstance(r, StopOrderRequest) for r in submitted), (
        "whole-share path should attach a trailing stop, not a standalone "
        "fixed stop"
    )
