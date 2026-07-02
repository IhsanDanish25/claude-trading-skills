"""
Alpaca broker client — paper + live unified interface.
"""
from __future__ import annotations

import datetime
import logging
import time

import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

try:
    from alpaca.trading.requests import GetPortfolioHistoryRequest
except ImportError:
    GetPortfolioHistoryRequest = None
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce

from core.config import (
    ALPACA_API_KEY,
    ALPACA_BASE_URL,
    ALPACA_SECRET_KEY,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_SIZE_PCT,
    PAPER_TRADE,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
)
from core.notifier import send_error_alert

log = logging.getLogger(__name__)
ET  = pytz.timezone("America/New_York")

# Alpaca error code for "insufficient qty available" — raised when an OCO
# submission is rejected because the symbol's shares are already reserved
# by another open order.
_INSUFFICIENT_QTY_CODE = 40310000
_CANCEL_POLL_TIMEOUT = 5.0   # seconds to wait for cancelled orders to clear
_CANCEL_POLL_INTERVAL = 0.5
_OCO_RETRY_WAIT = 2.0        # seconds to wait before a single 40310000 retry


def _is_insufficient_qty_error(e: Exception) -> bool:
    try:
        if getattr(e, "code", None) == _INSUFFICIENT_QTY_CODE:
            return True
    except Exception:
        pass
    return str(_INSUFFICIENT_QTY_CODE) in str(e)


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

    def _open_orders_for(self, symbol: str) -> list:
        try:
            return [o for o in self.get_open_orders() if o.symbol == symbol]
        except Exception as e:
            log.warning(f"  list open orders for {symbol} failed: {e}")
            return []

    def _find_oco_legs(self, symbol: str):
        """Return (stop_leg, target_leg) for an existing OCO on symbol, or
        (None, None) if no complete OCO is currently open. Alpaca returns each
        OCO leg as its own order row (not nested under a parent), distinguished
        by order_class='oco' plus type — same convention tighten_stop relies on."""
        stop_leg = target_leg = None
        for o in self._open_orders_for(symbol):
            if str(getattr(o, "side", "")).lower() != "sell":
                continue
            if "oco" not in str(getattr(o, "order_class", "")).lower():
                continue
            order_type = str(getattr(o, "type", "")).lower()
            if "stop" in order_type:
                stop_leg = o
            elif "limit" in order_type:
                target_leg = o
        return stop_leg, target_leg

    def _cancel_symbol_orders(self, symbol: str,
                               timeout: float = _CANCEL_POLL_TIMEOUT,
                               interval: float = _CANCEL_POLL_INTERVAL) -> bool:
        """Cancel every open order for symbol and poll until Alpaca releases
        the reserved shares (or timeout). Returns True once no open orders
        remain for the symbol."""
        open_orders = self._open_orders_for(symbol)
        if not open_orders:
            return True
        for o in open_orders:
            try:
                self.trade.cancel_order_by_id(o.id)
                log.info(f"  Cancelled order {str(o.id)[:8]} ({symbol} {o.side} "
                         f"{o.type}) to release shares")
            except Exception as e:
                log.warning(f"  Cancel order {o.id} ({symbol}) failed: {e}")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(interval)
            if not self._open_orders_for(symbol):
                return True
        log.warning(f"  Timed out waiting for {symbol} orders to cancel — "
                    f"shares may still be reserved")
        return False

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

        A symbol's shares can already be tied up by some other open order (a
        stale plain SELL LIMIT, a leftover bracket leg, etc.) — submitting a
        fresh OCO on top of that is rejected by Alpaca with error 40310000
        ("insufficient qty available"). To avoid that:
          1. Skip entirely if a matching OCO (same stop, target, qty) is
             already open for the symbol — no need to churn orders.
          2. Otherwise cancel every open order for the symbol and poll until
             Alpaca confirms the shares are released, then submit.
          3. If Alpaca still rejects with 40310000, wait and retry once more.
          4. If it still fails, log an ERROR and send an alert email — this
             position would otherwise be left with no protection.

        OCO is atomic, so both legs attach together or neither does. Returns
        (stop_attached, target_attached) — both the same bool — to preserve the
        caller contract."""
        stop_leg, target_leg = self._find_oco_legs(symbol)
        if stop_leg is not None and target_leg is not None:
            leg_qty = int(float(getattr(stop_leg, "qty", 0)
                                 or getattr(target_leg, "qty", 0) or 0))
            stop_price = getattr(stop_leg, "stop_price", None)
            target_price = getattr(target_leg, "limit_price", None)
            if (stop_price is not None and abs(float(stop_price) - stop) <= 0.01
                    and target_price is not None and abs(float(target_price) - target) <= 0.01
                    and leg_qty == qty):
                log.info(f"  ↳ OCO already correct for {symbol} "
                         f"(stop=${stop:.2f} target=${target:.2f} x{qty}) — skip")
                return True, True

        attempts = 2
        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._cancel_symbol_orders(symbol)
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
                last_err = e
                if _is_insufficient_qty_error(e) and attempt < attempts:
                    log.warning(f"  ↳ OCO attach {symbol} hit 40310000 (insufficient qty) "
                                f"— re-cancelling and retrying (attempt {attempt}/{attempts})")
                    time.sleep(_OCO_RETRY_WAIT)
                    continue
                break

        log.error(f"  ↳ OCO attach FAILED [{symbol}]: {last_err}")
        if last_err is not None and _is_insufficient_qty_error(last_err):
            try:
                send_error_alert(
                    routine="attach_stop_target",
                    error=f"{symbol}: OCO attach failed after cancel + retry — {last_err}",
                )
            except Exception as notify_err:
                log.error(f"  ↳ failed to send alert email for {symbol}: {notify_err}")
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

    def tighten_stop(self, symbol: str, new_stop: float, target: float | None = None) -> bool:
        """Replace the open stop-loss for symbol with a tighter stop price.

        Handles three cases:
          1. Standalone stop order (type=stop, side=sell) — replace directly.
          2. OCO child stop-loss leg (type=stop, returned as separate order) — replace.
          3. No stop order at all — e.g. the position's shares are held by a
             plain SELL LIMIT (no stop_price), which happens when an earlier
             OCO attach was skipped or rejected. Replacing has nothing to
             target, so cancel whatever's open and rebuild a full OCO with the
             tightened stop instead of leaving the position unprotected.
        Returns True only if the stop was actually tightened on Alpaca (either
        by replace, or by a full OCO rebuild).

        Note: Alpaca doesn't expose stop_price on the OCO parent — the stop-loss
        is a child order returned as type=stop. We match by type instead."""
        try:
            open_orders = self.get_open_orders()
            log.info(f"tighten_stop: {len(open_orders)} open orders total")
            # Log all orders for this symbol for debugging
            for o in open_orders:
                if o.symbol == symbol:
                    log.info(f"  Order: id={o.id} type={o.type} side={o.side} "
                              f"order_class={getattr(o, 'order_class', 'n/a')} "
                              f"stop_price={getattr(o, 'stop_price', 'n/a')} "
                              f"limit_price={getattr(o, 'limit_price', 'n/a')}")

            # Match stop orders for this symbol (handles both standalone stops
            # and OCO child stop-loss legs, which Alpaca returns as separate rows)
            candidates = []
            for o in open_orders:
                if o.symbol != symbol:
                    continue
                if str(getattr(o, "side", "")).lower() != "sell":
                    continue
                order_type = str(getattr(o, "type", "")).lower()
                if "stop" not in order_type:
                    continue
                candidates.append(o)

            if not candidates:
                log.warning("tighten_stop: no open stop order for %s — "
                            "rebuilding full OCO instead of leaving it unprotected", symbol)
                return self._rebuild_oco_stop(symbol, new_stop, target)
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

    def _rebuild_oco_stop(self, symbol: str, new_stop: float, target: float | None) -> bool:
        """No stop leg found for symbol's open orders — cancel whatever is
        open (releasing the shares) and place a fresh OCO with the tightened
        stop, so the position is never left with only a take-profit or a
        plain sell order and no downside protection."""
        pos = self.get_position(symbol)
        if pos is None:
            log.warning("tighten_stop: no position in %s to rebuild OCO for", symbol)
            return False
        qty = int(float(pos.qty))
        if qty < 1:
            return False

        if target is None:
            _, target_leg = self._find_oco_legs(symbol)
            target_price = getattr(target_leg, "limit_price", None) if target_leg else None
            if target_price is not None:
                target = float(target_price)
            else:
                entry = float(pos.avg_entry_price)
                target = round(entry * (1 + TAKE_PROFIT_PCT), 2)
                log.warning("tighten_stop: no target supplied/found for %s — "
                            "defaulting to entry-based target $%.2f", symbol, target)

        stop_attached, _ = self.attach_stop_target(symbol, qty, new_stop, target)
        if stop_attached:
            log.info("tighten_stop: rebuilt OCO for %s — stop $%.2f / target $%.2f",
                      symbol, new_stop, target)
        return stop_attached

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
