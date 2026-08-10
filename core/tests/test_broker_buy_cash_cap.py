"""Unit tests for BrokerClient.buy() cash/buying-power guardrail.

buy() previously sized orders off equity (portfolio_value, which includes
the market value of existing positions) and clamped only against
MAX_POSITION_SIZE_PCT of that equity. On an account that is already mostly
invested, equity can far exceed actual spendable cash, so a qty sized off
equity alone could exceed buying power and get rejected — or draw on
margin — at submission time. These tests exercise the added guardrail that
clamps (or blocks) the order against BrokerClient.buying_power().

Pure-logic tests: BrokerClient is built via object.__new__ to skip
TradingClient/StockHistoricalDataClient construction (no network, no API
keys), and a fake trade client stands in for order submission/polling.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.broker import BrokerClient


class _FakeTradeClient:
    def __init__(self, fill_price: float):
        self._fill_price = fill_price
        self.submitted = []

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="order-1")

    def get_order_by_id(self, order_id):
        req = self.submitted[-1]
        if getattr(req, "notional", None):
            filled_qty = round(req.notional / self._fill_price, 9)
        else:
            filled_qty = req.qty
        return SimpleNamespace(filled_avg_price=self._fill_price, filled_qty=filled_qty)


def _make_broker(
    *,
    ref_price: float,
    equity: float,
    buying_power: float,
    existing_position=None,
    position_count: int = 0,
) -> BrokerClient:
    broker = object.__new__(BrokerClient)
    broker.trade = _FakeTradeClient(fill_price=ref_price)
    broker.get_price = lambda symbol: ref_price
    broker.portfolio_value = lambda: equity
    broker.buying_power = lambda: buying_power
    broker.get_position = lambda symbol: existing_position
    broker.position_count = lambda: position_count
    # These tests exercise the cash-cap guardrail specifically; the spread
    # gate has its own dedicated coverage in test_broker_spread_check.py.
    broker.check_spread = lambda symbol: {"ok": True, "reason": None, "spread_pct": 0.001}
    broker.is_market_open = lambda: True
    broker.attach_stop_target = lambda symbol, qty, stop, target: (True, True)
    return broker


def test_buy_unaffected_when_cash_covers_sized_order():
    # equity=$100k, 5% cap -> $5k notional cap; plenty of cash available.
    broker = _make_broker(ref_price=50.0, equity=100_000.0, buying_power=80_000.0)

    result = broker.buy("AAPL", shares=20, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result.get("blocked") is None
    assert result["qty"] == 20
    assert broker.trade.submitted[-1].qty == 20


def test_buy_clamped_when_order_exceeds_available_cash():
    # Requested 20 shares @ $50 = $1000, but only $300 of buying power exists.
    broker = _make_broker(ref_price=50.0, equity=100_000.0, buying_power=300.0)

    result = broker.buy("AAPL", shares=20, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result.get("blocked") is None
    assert result["qty"] == 6  # int(300 / 50)
    assert broker.trade.submitted[-1].qty == 6


def test_buy_blocked_when_cash_cannot_afford_one_share():
    broker = _make_broker(ref_price=50.0, equity=100_000.0, buying_power=10.0)

    result = broker.buy("AAPL", shares=20, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result == {"blocked": True, "reason": "insufficient_cash"}
    assert broker.trade.submitted == []


def test_buy_cash_cap_applies_after_risk_parity_sizing():
    # No explicit shares: risk-parity/size-pct sizing picks a qty, which the
    # cash guardrail must still clamp down.
    broker = _make_broker(ref_price=100.0, equity=100_000.0, buying_power=250.0)

    result = broker.buy("AAPL", stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result.get("blocked") is None
    assert result["qty"] == 2  # int(250 / 100)
    assert broker.trade.submitted[-1].qty == 2


def test_buy_uses_dollar_amount_as_additional_size_cap(monkeypatch):
    # Risk-parity sizing alone would pick a much larger qty than the
    # strategy's intended dollar_amount; dollar_amount must additionally cap it
    # so per-strategy *_SIZE_PCT values (e.g. PEAD_SIZE_PCT) actually take effect.
    monkeypatch.setattr("core.broker.RISK_PCT", 0.10)  # generous risk budget
    monkeypatch.setattr("core.broker.MAX_POSITION_SIZE_PCT", 0.50)  # generous cap
    broker = _make_broker(ref_price=100.0, equity=100_000.0, buying_power=100_000.0)

    result = broker.buy("AAPL", dollar_amount=1_000.0, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result.get("blocked") is None
    assert result["qty"] == 10  # int(1000 / 100), not the much larger risk-parity qty
    assert broker.trade.submitted[-1].qty == 10


def test_buy_dollar_amount_does_not_relax_existing_caps(monkeypatch):
    # A generous dollar_amount must not let the order exceed the tighter of
    # the risk-parity / MAX_POSITION_SIZE_PCT ceilings — dollar_amount only tightens.
    monkeypatch.setattr("core.broker.RISK_PCT", 0.01)
    monkeypatch.setattr("core.broker.MAX_POSITION_SIZE_PCT", 0.05)
    broker = _make_broker(ref_price=100.0, equity=100_000.0, buying_power=100_000.0)

    result = broker.buy("AAPL", dollar_amount=50_000.0, stop_loss_pct=0.05, take_profit_pct=0.10)

    # risk_qty = int(100000*0.01/(100*0.05)) = 200; size_qty = int(100000*0.05/100) = 50
    # qty = min(200, 50) = 50 — the $50,000 dollar_amount cap (500 shares) doesn't bind.
    assert result.get("blocked") is None
    assert result["qty"] == 50
    assert broker.trade.submitted[-1].qty == 50


def test_buy_without_dollar_amount_unaffected():
    # Backward-compat: omitting dollar_amount still falls back to pure
    # risk-parity/size-cap sizing.
    broker = _make_broker(ref_price=100.0, equity=100_000.0, buying_power=100_000.0)

    result = broker.buy("AAPL", stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result.get("blocked") is None
    assert broker.trade.submitted[-1].qty == result["qty"]


def test_buy_falls_back_to_fractional_when_whole_share_unaffordable():
    # A dollar_amount request whose share price exceeds the affordable
    # whole-share budget submits a fractional (notional) order instead of
    # blocking. This depends on attach_stop_target using a DAY time-in-force
    # for fractional qty (Alpaca rejects GTC on fractional orders, error
    # 42210000) — see test_broker_attach_stop_target_tif.py. Mirrors the live
    # 2026-07-28 AAPL/TXN case (share $339, ~$26 budget) that previously
    # caused immediate-flatten churn under the old GTC-only attach.
    broker = _make_broker(ref_price=339.0, equity=264.0, buying_power=205.0)

    result = broker.buy("AAPL", dollar_amount=26.0, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result.get("blocked") is None
    submitted = broker.trade.submitted[-1]
    # notional clamps to the 8% position cap (0.08 * $264 = $21.12), the
    # tightest of dollar_amount/remaining_cap/available_cash here.
    assert submitted.notional == 21.12
    assert result["qty"] == round(21.12 / 339.0, 9)


def test_buy_blocks_fractional_below_min_notional():
    # A dollar_amount so small the fractional order wouldn't clear
    # MIN_FRACTIONAL_NOTIONAL still blocks rather than submitting a
    # near-zero order that just bleeds spread/fees.
    broker = _make_broker(ref_price=339.0, equity=264.0, buying_power=205.0)

    result = broker.buy("AAPL", dollar_amount=2.0, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result == {"blocked": True, "reason": "insufficient_cash"}
    assert broker.trade.submitted == []


def test_buy_blocks_when_no_dollar_amount_and_whole_share_unaffordable():
    # Fractional fallback only applies to dollar_amount requests (the caller
    # has an explicit notional budget in mind). A bare shares= request that
    # can't afford 1 whole share still blocks — there's no notional figure to
    # size a fractional order against.
    broker = _make_broker(ref_price=50.0, equity=100_000.0, buying_power=10.0)

    result = broker.buy("AAPL", shares=20, stop_loss_pct=0.05, take_profit_pct=0.10)

    assert result == {"blocked": True, "reason": "insufficient_cash"}
    assert broker.trade.submitted == []
