"""
Alpaca broker client — paper + live unified interface.
"""
from __future__ import annotations
import logging
import datetime
import time
import pytz

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
    GetOrdersRequest,
    ClosePositionRequest,
    ReplaceOrderRequest,
)
try:
    from alpaca.trading.requests import GetPortfolioHistoryRequest
except ImportError:
    GetPortfolioHistoryRequest = None
from alpaca.trading.enums import (
    OrderSide, TimeInForce, QueryOrderStatus, OrderClass
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame

from core.config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    PAPER_TRADE, MAX_POSITION_SIZE_PCT, MAX_OPEN_POSITIONS,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT,
)

log = logging.getLogger(__name__)
ET  = pytz.timezone("America/New_York")


class BrokerClient:
    def __init__(self):
        self.trade = TradingClient(
            ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADE
        )
        self.data = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        mode = "PAPER" if PAPER_TRADE else "LIVE"
        log.info(f"Broker init [{mode}] → {ALPACA_BASE_URL}")

    # ── Account ───────────────────────────────────────────────────────────────
    def get_account(self):
        return self.trade.get_account()

    def buying_power(self) -> float:
        return float(self.get_account().buying_power)

    def portfolio_value(self) -> float:
        return float(self.get_account().portfolio_value)

    def cash(self) -> float:
        return float(self.get_account().cash)

    # ── Positions ─────────────────────────────────────────────────────────────
    def get_positions(self) -> list:
        return self.trade.get_all_positions()

    def get_position(self, symbol: str):
        try:
            return self.trade.get_open_position(symbol)
        except Exception:
            return None

    def position_count(self) -> int:
        return len(self.get_positions())

    # ── Orders ────────────────────────────────────────────────────────────────
    def get_open_orders(self):
        # FIX: use QueryOrderStatus not OrderStatus for filtering
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        return self.trade.get_orders(filter=req)

    def cancel_all_orders(self):
        self.trade.cancel_orders()
        log.info("All orders cancelled")

    # ── Market data ───────────────────────────────────────────────────────────
    def get_bars(self, symbols: list, timeframe: TimeFrame, days: int = 60):
        end   = datetime.datetime.now(ET)
        start = end - datetime.timedelta(days=days)
        req   = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=timeframe,
            start=start,
            end=end,
        )
        return self.data.get_stock_bars(req)

    def get_latest_quotes(self, symbols: list):
        req = StockLatestQuoteRequest(symbol_or_symbols=symbols)
        return self.data.get_stock_latest_quote(req)

    def get_price(self, symbol: str) -> float:
        """Best-effort current price, robust to one-sided / crossed / stale quotes.

        Anchors on the last trade price, and uses the quote midpoint only when
        both sides are valid (bid > 0, ask >= bid) AND the midpoint is within
        10% of the last trade. This rejects after-hours quotes such as
        bid=275/ask=0 or bid=275/ask=0.5, where ``(bid + ask) / 2`` would halve
        the price and corrupt sizing and stop/target levels. Falls back to the
        last trade otherwise.
        """
        last = None
        try:
            t = self.data.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol)
            )[symbol]
            last = float(t.price)
        except Exception as e:
            log.warning("get_price last-trade failed for %s: %s", symbol, e)
        try:
            q = self.get_latest_quotes([symbol])[symbol]
            bid = float(getattr(q, "bid_price", 0) or 0)
            ask = float(getattr(q, "ask_price", 0) or 0)
            if bid > 0 and ask >= bid:
                mid = (bid + ask) / 2
                if last is None or abs(mid - last) <= 0.10 * last:
                    return mid
        except Exception as e:
            log.warning("get_price quote failed for %s: %s", symbol, e)
        if last is not None:
            return last
        raise RuntimeError(f"no usable price for {symbol}")

    # ── Trade execution ───────────────────────────────────────────────────────
    def attach_stop_target(self, symbol: str, qty: int,
                           stop: float, target: float) -> tuple[bool, bool]:
        """Attach a protective exit as a single OCO (one-cancels-other) order:
        a take-profit limit and a stop-loss that share the same shares. When
        either leg fills, Alpaca cancels the other — so both can coexist on one
        position (unlike two independent full-qty SELL orders, where the first
        reserves the shares and the second is rejected).

        OCO is atomic, so both legs attach together or neither does. Returns
        (stop_attached, target_attached) — both the same bool — to preserve the
        caller contract."""
        try:
            self.trade.submit_order(LimitOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC, order_class=OrderClass.OCO,
                take_profit=TakeProfitRequest(limit_price=target),
                stop_loss=StopLossRequest(stop_price=stop),
            ))
            log.info(f"  ↳ OCO attached: stop @ ${stop:.2f} / target @ ${target:.2f} x{qty} [{symbol}]")
            return True, True
        except Exception as e:
            log.error(f"  ↳ OCO attach FAILED [{symbol}]: {e}")
            return False, False

    def buy(self, symbol: str, dollar_amount: float = None, shares: int = None,
            stop_loss_pct: float = STOP_LOSS_PCT,
            take_profit_pct: float = TAKE_PROFIT_PCT) -> dict:
        """
        Simple market BUY with hard position-sizing guardrails, then attach a
        protective OCO exit (stop + target) priced off the REAL fill price.

        Guardrails enforced before every order:
        1. Position count must be below MAX_OPEN_POSITIONS (new symbols only).
        2. Order value is clamped to equity * MAX_POSITION_SIZE_PCT, accounting
           for any existing position in the same symbol.

        Pass either dollar_amount OR shares. Returns qty, price (fill basis),
        stop, target, and stop_attached / target_attached bools.
        """
        ref_price = self.get_price(symbol)
        equity = self.portfolio_value()
        max_position_dollars = equity * MAX_POSITION_SIZE_PCT

        # ── Guardrail 1: enforce MAX_OPEN_POSITIONS ──────────────────────────
        existing_pos = self.get_position(symbol)
        if existing_pos is None and self.position_count() >= MAX_OPEN_POSITIONS:
            log.warning(
                "BUY %s BLOCKED — already at %d/%d open positions",
                symbol, self.position_count(), MAX_OPEN_POSITIONS,
            )
            return {"blocked": True, "reason": "max_open_positions"}

        # ── Guardrail 2: clamp order to per-position cap ─────────────────────
        existing_value = 0.0
        if existing_pos is not None:
            existing_value = abs(float(existing_pos.market_value or 0))
        remaining_cap = max(0.0, max_position_dollars - existing_value)

        if remaining_cap <= 0:
            log.warning(
                "BUY %s BLOCKED — existing position $%.0f already at/above "
                "%.1f%% cap ($%.0f)",
                symbol, existing_value, MAX_POSITION_SIZE_PCT * 100,
                max_position_dollars,
            )
            return {"blocked": True, "reason": "position_size_cap"}

        if dollar_amount:
            qty = max(1, int(dollar_amount / ref_price))
        elif shares:
            qty = shares
        else:
            qty = max(1, int(remaining_cap / ref_price))

        order_value = qty * ref_price
        if order_value > remaining_cap:
            clamped_qty = max(1, int(remaining_cap / ref_price))
            log.warning(
                "BUY %s CLAMPED — requested %d shares ($%.0f) exceeds "
                "%.1f%% cap; reduced to %d shares ($%.0f)",
                symbol, qty, order_value,
                MAX_POSITION_SIZE_PCT * 100,
                clamped_qty, clamped_qty * ref_price,
            )
            qty = clamped_qty

        if qty < 1:
            log.warning("BUY %s BLOCKED — clamped qty to 0", symbol)
            return {"blocked": True, "reason": "position_size_cap"}

        # 1. Simple market BUY
        order = self.trade.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        ))
        log.info(f"BUY {symbol} x{qty} (market) submitted [{str(order.id)[:8]}]")

        # 2. Poll for the actual fill price (up to ~5s)
        fill_price = None
        filled_qty = qty
        for _ in range(10):
            try:
                o = self.trade.get_order_by_id(order.id)
            except Exception as e:
                log.warning(f"poll order {symbol} failed: {e}")
                break
            if o.filled_avg_price:
                fill_price = float(o.filled_avg_price)
                if o.filled_qty:
                    filled_qty = int(float(o.filled_qty))
                break
            time.sleep(0.5)

        # 3. Compute stop/target from the REAL fill price (fallback to reference)
        basis = fill_price if fill_price else ref_price
        if fill_price is None:
            log.warning(f"{symbol} not filled within 5s — using reference ${ref_price:.2f} for stop/target")
        stop   = round(basis * (1 - stop_loss_pct), 2)
        target = round(basis * (1 + take_profit_pct), 2)

        # 4. Attach protective stop-loss + take-profit (each its own try/except)
        stop_attached, target_attached = self.attach_stop_target(
            symbol, filled_qty, stop, target
        )
        log.info(f"BUY {symbol} x{filled_qty} @ ${basis:.2f} | SL={stop} TP={target} "
                 f"| stop_attached={stop_attached} target_attached={target_attached}")

        return {
            "order": order, "qty": filled_qty, "price": basis,
            "stop": stop, "target": target,
            "stop_attached": stop_attached, "target_attached": target_attached,
        }

    def sell(self, symbol: str, qty: int = None) -> dict:
        """Market sell. qty=None → close entire position."""
        if qty is None:
            pos = self.get_position(symbol)
            if not pos:
                log.warning(f"No position in {symbol}")
                return {}
            qty = int(float(pos.qty))

        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self.trade.submit_order(req)
        log.info(f"SELL {symbol} x{qty}")
        return {"order": order, "qty": qty}

    def close_position(self, symbol: str):
        try:
            self.trade.close_position(symbol)
            log.info(f"Closed {symbol}")
        except Exception as e:
            log.error(f"Close {symbol} failed: {e}")

    def close_all_positions(self):
        # FIX: correct alpaca-py signature
        self.trade.close_all_positions(cancel_orders=True)
        log.warning("ALL POSITIONS CLOSED")

    def tighten_stop(self, symbol: str, new_stop: float) -> bool:
        """Replace the open stop-loss for symbol with a tighter stop price.

        Handles two cases:
          1. Standalone stop order (type=stop, side=sell) — replace directly.
          2. OCO parent order (order_class=oco) — replace by id, which updates
             both legs (take_profit limit + stop_loss stop) in one call.
        Returns True only if the order was actually replaced on Alpaca."""
        try:
            open_orders = self.get_open_orders()
            candidates = [
                o for o in open_orders
                if o.symbol == symbol
                and "sell" in str(getattr(o, "side", "")).lower()
                and (
                    "oco" in str(getattr(o, "order_class", "")).lower()
                    or "stop" in str(getattr(o, "type", "")).lower()
                )
            ]
            if not candidates:
                log.warning("tighten_stop: no open stop/OCO order for %s", symbol)
                return False
            order = candidates[0]
            old_stop = getattr(order, "stop_price", None)
            old_label = f"${old_stop:.2f}" if isinstance(old_stop, (int, float)) else str(old_stop or "?")
            self.trade.replace_order_by_id(
                str(order.id),
                ReplaceOrderRequest(stop_price=new_stop),
            )
            log.info("Stop tightened %s: %s → $%.2f", symbol, old_label, new_stop)
            return True
        except Exception as e:
            log.error("tighten_stop %s failed: %s", symbol, e)
            return False

    def get_portfolio_history(self, period: str = "1W"):
        try:
            if GetPortfolioHistoryRequest is None:
                log.warning("GetPortfolioHistoryRequest not available in this alpaca-py version")
                return None
            req = GetPortfolioHistoryRequest(period=period, timeframe="1D")
            try:
                return self.trade.get_portfolio_history(history_filter=req)
            except TypeError:
                return self.trade.get_portfolio_history(req)
        except Exception as e:
            log.error("Portfolio history fail: %s", e)
            return None

    # ── Market status ─────────────────────────────────────────────────────────
    def is_market_open(self) -> bool:
        clock = self.trade.get_clock()
        return clock.is_open

    def next_open(self):
        return self.trade.get_clock().next_open

    def next_close(self):
        return self.trade.get_clock().next_close
