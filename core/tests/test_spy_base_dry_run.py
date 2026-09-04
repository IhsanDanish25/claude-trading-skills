"""DRY_RUN must reach core/spy_base.py too.

Every strategy runner in market_open.py (crypto included) calls
free_cash_for_pead() before its own broker.buy() -- and broker.buy() already
short-circuits under DRY_RUN (core/broker.py's _dry_run_fill). But
free_cash_for_pead() and rebalance_to_spy() submitted real Alpaca orders
unconditionally, with no DRY_RUN check anywhere in this module: DRY_RUN=true
would still sell/buy real SPY shares to free cash or rebalance the base
position, and still email a real-looking SELL/BUY confirmation for it --
defeating the "no order ever reaches Alpaca" guarantee DRY_RUN is documented
to provide (core/config.py).

Pure-logic tests: no real BrokerClient, just a duck-typed fake exposing the
methods these functions call, mirroring
core/tests/test_spy_base_rebalance_cash_cap.py.
"""

from __future__ import annotations

from types import SimpleNamespace

from core import spy_base


class _ExplodingTradeClient:
    """submit_order must never be called while DRY_RUN is on."""

    def submit_order(self, req):
        raise AssertionError("submit_order should not be called in DRY_RUN mode")


class _FakeBroker:
    def __init__(self, *, equity, buying_power, spy_price, spy_qty=0.0,
                 spy_value=0.0, cash=None):
        self.trade = _ExplodingTradeClient()
        self._equity = equity
        self._buying_power = buying_power
        self._spy_price = spy_price
        self._spy_qty = spy_qty
        self._spy_value = spy_value
        self._cash = cash if cash is not None else equity

    def portfolio_value(self) -> float:
        return self._equity

    def cash(self) -> float:
        return self._cash

    def buying_power(self) -> float:
        return self._buying_power

    def get_positions(self) -> list:
        return []

    def get_position(self, symbol: str):
        if symbol == "SPY" and self._spy_qty:
            return SimpleNamespace(qty=self._spy_qty, market_value=self._spy_value,
                                    avg_entry_price=self._spy_price)
        return None

    def get_price(self, symbol: str) -> float:
        return self._spy_price

    def get_open_orders(self):
        return []


def _patch_spy_config(monkeypatch, *, dry_run: bool):
    monkeypatch.setattr(spy_base.config, "SPY_BASE_ENABLED", True)
    monkeypatch.setattr(spy_base.config, "SPY_CASH_RESERVE_PCT", 0.10)
    monkeypatch.setattr(spy_base.config, "SPY_REBALANCE_BAND", 0.05)
    monkeypatch.setattr(spy_base.config, "SPY_MAX_PCT", 0.93)
    monkeypatch.setattr(spy_base.config, "DRY_RUN", dry_run)
    alerts_sent = []
    monkeypatch.setattr(spy_base, "send_trade_alert", lambda **k: alerts_sent.append(k))
    return alerts_sent


def test_rebalance_buy_does_not_submit_or_email_under_dry_run(monkeypatch):
    alerts_sent = _patch_spy_config(monkeypatch, dry_run=True)
    broker = _FakeBroker(equity=100_000.0, buying_power=100_000.0, spy_price=590.0)

    result = spy_base.rebalance_to_spy(broker)

    assert result["action"] == "dry_run"
    assert alerts_sent == []


def test_rebalance_sell_does_not_submit_or_email_under_dry_run(monkeypatch):
    alerts_sent = _patch_spy_config(monkeypatch, dry_run=True)
    # Overweight SPY (holding more than target) -> sell branch.
    broker = _FakeBroker(
        equity=100_000.0, buying_power=100_000.0, spy_price=590.0,
        spy_qty=200.0, spy_value=118_000.0,
    )

    result = spy_base.rebalance_to_spy(broker)

    assert result["action"] == "dry_run"
    assert alerts_sent == []


def test_free_cash_for_pead_does_not_submit_or_email_under_dry_run(monkeypatch):
    alerts_sent = _patch_spy_config(monkeypatch, dry_run=True)
    # Not enough cash on hand, but SPY is held -> would normally sell it.
    broker = _FakeBroker(
        equity=10_000.0, buying_power=10_000.0, spy_price=590.0,
        spy_qty=10.0, spy_value=5_900.0, cash=5.0,
    )

    ok = spy_base.free_cash_for_pead(broker, amount_needed=100.0)

    # Reports success so the caller's own broker.buy() still runs -- and
    # that call independently no-ops under DRY_RUN (core/broker.py).
    assert ok is True
    assert alerts_sent == []


def test_free_cash_for_pead_still_sells_when_dry_run_is_off(monkeypatch):
    """Sanity check the guard is DRY_RUN-gated, not unconditional."""
    alerts_sent = _patch_spy_config(monkeypatch, dry_run=False)
    broker = _FakeBroker(
        equity=10_000.0, buying_power=10_000.0, spy_price=590.0,
        spy_qty=10.0, spy_value=5_900.0, cash=5.0,
    )
    # A real client here (not the exploding fake) since DRY_RUN is off and a
    # submit_order call is expected.
    submitted = []

    class _RealTradeClient:
        def submit_order(self, req):
            submitted.append(req)
            return SimpleNamespace(id="order-1", filled_avg_price=None)

    broker.trade = _RealTradeClient()

    ok = spy_base.free_cash_for_pead(broker, amount_needed=100.0)

    assert ok is True
    assert len(submitted) == 1
    assert len(alerts_sent) == 1
