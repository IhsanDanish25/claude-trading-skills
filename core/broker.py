"""
Alpaca broker client — paper + live unified interface.
"""
from __future__ import annotations
import logging
import datetime
import pytz

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOrdersRequest,
    ClosePositionRequest,
    StopLossRequest,
    TakeProfitRequest,
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
    PAPER_TRADE, MAX_POSITION_SIZE_PCT, STOP_LOSS_PCT, TAKE_PROFIT_PCT
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
        """Mid-quote price, robust to one-sided/empty quotes.

        When the market is closed (or pre/post-market) one side of the quote is
        often 0; ``(ask + bid) / 2`` would then return half the real price,
        which corrupts position sizing and bracket stop/target levels. Use the
        midpoint only when both sides are valid, otherwise fall back to the live
        side, then to the last trade price.
        """
        try:
            q = self.get_latest_quotes([symbol])[symbol]
            bid = float(getattr(q, "bid_price", 0) or 0)
            ask = float(getattr(q, "ask_price", 0) or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            if ask > 0:
                return ask
            if bid > 0:
                return bid
        except Exception as e:
            log.warning("get_price quote failed for %s: %s — falling back to last trade", symbol, e)
        # Both sides unusable (or quote error) → last trade price
        t = self.data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol)
        )[symbol]
        return float(t.price)

    # ── Trade execution ───────────────────────────────────────────────────────
    def buy(self, symbol: str, dollar_amount: float = None, shares: int = None,
            stop_loss_pct: float = STOP_LOSS_PCT,
            take_profit_pct: float = TAKE_PROFIT_PCT) -> dict:
        """
        Market buy with bracket (stop + target).
        Falls back to simple market order if bracket is rejected.
        Pass either dollar_amount OR shares.
        """
        price = self.get_price(symbol)

        if dollar_amount:
            qty = max(1, int(dollar_amount / price))
        elif shares:
            qty = shares
        else:
            pv  = self.portfolio_value()
            qty = max(1, int((pv * MAX_POSITION_SIZE_PCT) / price))

        stop   = round(price * (1 - stop_loss_pct), 2)
        target = round(price * (1 + take_profit_pct), 2)

        try:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=stop),
                take_profit=TakeProfitRequest(limit_price=target),
            )
            order = self.trade.submit_order(req)
            log.info(f"BUY {symbol} x{qty} @ ~{price:.2f} | SL={stop} TP={target} [bracket]")
        except Exception as e:
            log.warning(f"Bracket order rejected for {symbol}: {e} — falling back to simple market order")
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            order = self.trade.submit_order(req)
            log.warning(f"BUY {symbol} x{qty} @ ~{price:.2f} [NO STOP — bracket rejected, simple order used]")
            stop   = 0.0
            target = 0.0

        return {"order": order, "qty": qty, "price": price, "stop": stop, "target": target}

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
        """Replace the open stop-loss child order for symbol with a tighter stop price."""
        try:
            open_orders = self.get_open_orders()
            stop_orders = [
                o for o in open_orders
                if o.symbol == symbol
                and "stop" in str(o.type).lower()
                and "sell" in str(o.side).lower()
            ]
            if not stop_orders:
                log.warning("tighten_stop: no open stop order for %s", symbol)
                return False
            order = stop_orders[0]
            old_stop = getattr(order, "stop_price", "?")
            self.trade.replace_order_by_id(
                str(order.id),
                ReplaceOrderRequest(stop_price=new_stop),
            )
            log.info("Stop tightened %s: %s → $%.2f", symbol, old_stop, new_stop)
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
