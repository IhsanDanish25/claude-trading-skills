"""
Auto-trader — executes BUY orders from VCP signals via Alpaca paper account.

Uses bracket orders (stop-loss + take-profit) with position sizing from config:
  MAX_POSITION_SIZE_PCT  5%  per trade
  STOP_LOSS_PCT          2%  stop loss
  TAKE_PROFIT_PCT        6%  take profit
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from core.broker import BrokerClient
from core.config import (
    MAX_POSITION_SIZE_PCT,
    MAX_OPEN_POSITIONS,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    MIN_PRICE,
    MAX_PRICE,
    PAPER_TRADE,
)
from core.notifier import send_trade_alert

log = logging.getLogger(__name__)


@dataclass
class TradeResult:
    symbol: str
    success: bool
    qty: int = 0
    price: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    error: str = ""


class AutoTrader:
    def __init__(self, broker: BrokerClient | None = None):
        self.broker = broker or BrokerClient()
        mode = "PAPER" if PAPER_TRADE else "LIVE"
        log.info(f"AutoTrader init [{mode}] | "
                 f"max_pos={MAX_POSITION_SIZE_PCT:.0%} "
                 f"SL={STOP_LOSS_PCT:.0%} TP={TAKE_PROFIT_PCT:.0%}")

    def _available_slots(self) -> int:
        return MAX_OPEN_POSITIONS - self.broker.position_count()

    def _already_holding(self, symbol: str) -> bool:
        return self.broker.get_position(symbol) is not None

    def _has_pending_order(self, symbol: str) -> bool:
        open_orders = self.broker.get_open_orders()
        return any(o.symbol == symbol for o in open_orders)

    def _passes_price_filter(self, price: float) -> bool:
        return MIN_PRICE <= price <= MAX_PRICE

    def _position_dollar_amount(self, size_factor: float = 1.0) -> float:
        pv = self.broker.portfolio_value()
        return pv * MAX_POSITION_SIZE_PCT * size_factor

    def execute_signals(
        self,
        signals: list[dict],
        max_buys: int = 3,
        size_factor: float = 1.0,
        notify: bool = True,
    ) -> list[TradeResult]:
        """
        Execute BUY orders for VCP signals.

        signals: list of dicts with at least {symbol, score, action, reason}.
                 Only items with action=="BUY" are executed.
        max_buys: cap on new entries this run.
        size_factor: multiplier on MAX_POSITION_SIZE_PCT (e.g. 0.5 for defensive).
        notify: send email alerts on fills.

        Returns list of TradeResult for each attempted buy.
        """
        buy_signals = [s for s in signals if s.get("action") == "BUY"]
        if not buy_signals:
            log.info("No BUY signals to execute")
            return []

        slots = self._available_slots()
        if slots <= 0:
            log.warning("No position slots available — skipping all buys")
            return []

        cap = min(max_buys, slots)
        dollar_amount = self._position_dollar_amount(size_factor)
        log.info(f"Executing up to {cap} buys | ${dollar_amount:,.0f} each | "
                 f"size_factor={size_factor}")

        results: list[TradeResult] = []
        buys_done = 0

        for sig in buy_signals:
            if buys_done >= cap:
                break

            symbol = sig["symbol"]
            reason = sig.get("reason", "")

            if self._already_holding(symbol):
                log.info(f"  {symbol}: already holding — skip")
                results.append(TradeResult(symbol=symbol, success=False,
                                           error="already holding"))
                continue

            if self._has_pending_order(symbol):
                log.info(f"  {symbol}: pending order exists — skip")
                results.append(TradeResult(symbol=symbol, success=False,
                                           error="pending order"))
                continue

            try:
                price = self.broker.get_price(symbol)
            except Exception as exc:
                log.warning(f"  {symbol}: quote failed — {exc}")
                results.append(TradeResult(symbol=symbol, success=False,
                                           error=str(exc)))
                continue

            if not self._passes_price_filter(price):
                log.info(f"  {symbol}: price ${price:.2f} outside filter — skip")
                results.append(TradeResult(symbol=symbol, success=False,
                                           error="price filter"))
                continue

            try:
                order = self.broker.buy(
                    symbol,
                    dollar_amount=dollar_amount,
                    stop_loss_pct=STOP_LOSS_PCT,
                    take_profit_pct=TAKE_PROFIT_PCT,
                )
                tr = TradeResult(
                    symbol=symbol,
                    success=True,
                    qty=order["qty"],
                    price=order["price"],
                    stop=order["stop"],
                    target=order["target"],
                )
                results.append(tr)
                buys_done += 1
                log.info(f"  {symbol}: BUY {tr.qty} @ ${tr.price:.2f} | "
                         f"SL=${tr.stop} TP=${tr.target}")

                if notify:
                    send_trade_alert(
                        action="BUY",
                        ticker=symbol,
                        shares=tr.qty,
                        price=tr.price,
                        stop=tr.stop,
                        target=tr.target,
                        confidence=sig.get("score"),
                        reason=reason,
                    )

            except Exception as exc:
                log.error(f"  {symbol}: order failed — {exc}")
                results.append(TradeResult(symbol=symbol, success=False,
                                           error=str(exc)))

        filled = sum(1 for r in results if r.success)
        log.info(f"AutoTrader done | {filled}/{len(results)} orders filled")
        return results
