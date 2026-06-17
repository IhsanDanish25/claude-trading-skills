"""
Alpaca broker client — paper + live unified interface.
"""
import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest,
    StopLossRequest, TakeProfitRequest,
    GetOrdersRequest, GetAssetsRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, AssetClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
import datetime
import pytz

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
        req = GetOrdersRequest(status=OrderStatus.OPEN)
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
        quotes = self.get_latest_quotes([symbol])
        q = quotes[symbol]
        return float((q.ask_price + q.bid_price) / 2)

    # ── Trade execution ───────────────────────────────────────────────────────
    def buy(self, symbol: str, dollar_amount: float = None, shares: int = None,
             stop_loss_pct: float = STOP_LOSS_PCT,
             take_profit_pct: float = TAKE_PROFIT_PCT) -> dict:
        """
        Market buy with bracket (stop + target).
        Pass either dollar_amount OR shares.
        """
        price = self.get_price(symbol)

        if dollar_amount:
            qty = max(1, int(dollar_amount / price))
        elif shares:
            qty = shares
        else:
            # Default: MAX_POSITION_SIZE_PCT of portfolio
            pv  = self.portfolio_value()
            qty = max(1, int((pv * MAX_POSITION_SIZE_PCT) / price))

        stop   = round(price * (1 - stop_loss_pct), 2)
        target = round(price * (1 + take_profit_pct), 2)

        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class="bracket",
            stop_loss=StopLossRequest(stop_price=stop),
            take_profit=TakeProfitRequest(limit_price=target),
        )
        order = self.trade.submit_order(req)
        log.info(f"BUY {symbol} x{qty} @ ~{price:.2f} | SL={stop} TP={target}")
        return {"order": order, "qty": qty, "price": price, "stop": stop, "target": target}

    def sell(self, symbol: str, qty: int = None) -> dict:
        """Market sell. qty=None → close entire position."""
        if qty is None:
            pos = self.get_position(symbol)
            if not pos:
                log.warning(f"No position in {symbol}")
                return {}
            qty = int(pos.qty)

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
        self.trade.close_all_positions(cancel_orders=True)
        log.warning("ALL POSITIONS CLOSED")

    # ── Market status ─────────────────────────────────────────────────────────
    def is_market_open(self) -> bool:
        clock = self.trade.get_clock()
        return clock.is_open

    def next_open(self):
        return self.trade.get_clock().next_open

    def next_close(self):
        return self.trade.get_clock().next_close
