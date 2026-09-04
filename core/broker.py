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
    StopLimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

try:
    from alpaca.trading.requests import GetPortfolioHistoryRequest
except ImportError:
    GetPortfolioHistoryRequest = None
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestQuoteRequest,
    CryptoLatestTradeRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce

from core import cost_tracker, trade_logger
from core.config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    DRY_RUN,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_SIZE_PCT,
    MAX_SPREAD_PCT,
    PAPER_TRADE,
    RISK_PCT,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
)
from core.order_utils import order_field
from core.safe_oco_attach import safe_attach_oco

log = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

# Alpaca's live trading endpoint. alpaca-py's TradingClient picks the base
# URL from the `paper` bool, not from ALPACA_BASE_URL (no url_override is
# passed below) -- ALPACA_BASE_URL defaults to the *paper* URL in
# core/config.py and was never actually wired into this client, so logging
# it here previously claimed "[LIVE]" while printing a paper-api.alpaca.markets
# URL. This constant reflects what the client actually connects to.
LIVE_URL = "https://api.alpaca.markets"


class BrokerClient:
    """Alpaca broker client — LIVE TRADING ONLY.

    Every routine that instantiates this (market_open, midday_review,
    market_close, weekly_csp, scheduler, ...) trades the real account.
    Unlike auto_trader.py's own TradingClient(paper=config.PAPER_TRADE),
    `paper=False` here is intentional and hardcoded, not read from config.

    Because of that, ALPACA_PAPER_TRADE/ALPACA_PAPER being set to true is a
    live/paper mismatch, not a supported "run this on paper" toggle -- it
    would leave an operator watching a paper dashboard while every order
    placed through this client executes live. That exact class of mismatch
    ("silently missing" orders because the wrong account was being
    watched) is why __init__ now fails loudly instead of only logging
    "[LIVE]" and proceeding.
    """

    def __init__(self):
        if PAPER_TRADE:
            raise RuntimeError(
                "BrokerClient is LIVE-ONLY, but ALPACA_PAPER_TRADE/ALPACA_PAPER "
                "is set to true. This client always trades the live account "
                "regardless of that flag -- refusing to start rather than "
                "silently trading live while you believe you're on paper. "
                "Unset ALPACA_PAPER_TRADE (or set it to false) to confirm you "
                "intend to trade the live account."
            )
        self.trade = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
        self.data = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        self.crypto_data = CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        log.info(f"Broker init [LIVE] → {LIVE_URL}")

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
        except Exception as e:
            # Distinguish "no position" from real errors that callers need to handle.
            # 404 = Alpaca confirming zero position → treat as absent (safe default).
            # Anything else (network, rate-limit, auth) → surface to caller.
            err_str = str(e).lower()
            if "404" in err_str or "not found" in err_str or "does not exist" in err_str:
                return None
            log.warning("get_position %s: unexpected error %r", symbol, e)
            raise

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
        end = datetime.datetime.now(ET)
        start = end - datetime.timedelta(days=days)
        req = StockBarsRequest(
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
            t = self.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))[
                symbol
            ]
            last = float(t.price)
        except Exception as e:
            log.warning("get_price last-trade failed for %s: %s", symbol, e)
        try:
            q = self.get_latest_quotes([symbol])[symbol]
            bid = float(getattr(q, "bid_price", 0) or 0)
            ask = float(getattr(q, "ask_price", 0) or 0)
            if bid > 0 and ask >= bid:
                spread = ask - bid
                mid = (bid + ask) / 2
                mid_price = mid
                # Fix 19: reject wide spreads (stale quotes, low-liquidity names)
                if mid_price > 0 and spread / mid_price > MAX_SPREAD_PCT:
                    log.warning(
                        "get_price %s: spread %.2f%% (>$MAX_SPREAD_PCT=%.0f%%) "
                        "-- rejecting midpoint, falling back to last trade",
                        symbol,
                        spread / mid_price * 100,
                        MAX_SPREAD_PCT * 100,
                    )
                    # Widen the spread → mid is unreliable; fall through to last trade
                if (
                    mid_price > 0
                    and spread / mid_price <= MAX_SPREAD_PCT
                    and (last is None or abs(mid_price - last) <= 0.10 * last)
                ):
                    return mid_price
        except Exception as e:
            log.warning("get_price quote failed for %s: %s", symbol, e)
        if last is not None and last > 0:
            return last
        raise RuntimeError(f"no usable price for {symbol}")

    # ── Pre-trade spread gate ────────────────────────────────────────────────
    def check_spread(self, symbol: str) -> dict:
        """Hard pre-trade gate: block the entry when the bid-ask spread
        exceeds MAX_SPREAD_PCT of price. Unlike get_price()'s soft fallback
        (silently switches to last-trade), this BLOCKS the order outright.

        Returns {"ok": True/False, "reason": str|None, "spread_pct": float|None}.

        Fail-safe by design: missing quote data, a crossed/zero quote, or an
        API error all return ok=False (block the trade) rather than letting
        an unverified spread through. The one deliberate exception is a
        confirmed-closed market — quotes are stale/one-sided after hours by
        definition, so the check is skipped (ok=True, reason="market_closed")
        instead of false-positiving on every off-hours call.
        """
        try:
            market_open = self.is_market_open()
        except Exception as e:
            log.warning("check_spread %s: market clock check failed: %s", symbol, e)
            return {"ok": False, "reason": "clock_check_failed", "spread_pct": None}

        if not market_open:
            return {"ok": True, "reason": "market_closed", "spread_pct": None}

        try:
            q = self.get_latest_quotes([symbol])[symbol]
        except Exception as e:
            log.warning("check_spread %s: quote fetch failed: %s", symbol, e)
            return {"ok": False, "reason": "quote_unavailable", "spread_pct": None}

        bid = float(getattr(q, "bid_price", 0) or 0)
        ask = float(getattr(q, "ask_price", 0) or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return {"ok": False, "reason": "invalid_quote", "spread_pct": None}

        mid = (bid + ask) / 2
        if mid <= 0:
            return {"ok": False, "reason": "invalid_quote", "spread_pct": None}

        spread_pct = (ask - bid) / mid
        if spread_pct > MAX_SPREAD_PCT:
            return {"ok": False, "reason": "spread_too_wide", "spread_pct": spread_pct}
        return {"ok": True, "reason": None, "spread_pct": spread_pct}

    def check_crypto_spread(self, symbol: str) -> dict:
        """Crypto counterpart to check_spread — same MAX_SPREAD_PCT gate,
        against Alpaca's crypto quote endpoint. No market-closed skip:
        crypto trades 24/7, so the check always applies."""
        try:
            q = self.crypto_data.get_crypto_latest_quote(
                CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
            )[symbol]
        except Exception as e:
            log.warning("check_crypto_spread %s: quote fetch failed: %s", symbol, e)
            return {"ok": False, "reason": "quote_unavailable", "spread_pct": None}

        bid = float(getattr(q, "bid_price", 0) or 0)
        ask = float(getattr(q, "ask_price", 0) or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return {"ok": False, "reason": "invalid_quote", "spread_pct": None}

        mid = (bid + ask) / 2
        if mid <= 0:
            return {"ok": False, "reason": "invalid_quote", "spread_pct": None}

        spread_pct = (ask - bid) / mid
        if spread_pct > MAX_SPREAD_PCT:
            return {"ok": False, "reason": "spread_too_wide", "spread_pct": spread_pct}
        return {"ok": True, "reason": None, "spread_pct": spread_pct}

    def get_crypto_price(self, symbol: str) -> float:
        """Latest trade price for a crypto symbol (e.g. 'BTC/USD')."""
        try:
            resp = self.crypto_data.get_crypto_latest_trade(
                CryptoLatestTradeRequest(symbol_or_symbols=symbol)
            )
            return float(resp[symbol].price)
        except Exception as e:
            log.warning("get_crypto_price failed for %s: %s — trying bars", symbol, e)
        try:
            import datetime

            import pytz

            end = datetime.datetime.now(pytz.UTC)
            start = end - datetime.timedelta(hours=2)
            bars = self.crypto_data.get_crypto_bars(
                CryptoBarsRequest(
                    symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start, end=end
                )
            )
            df = bars[symbol].df
            return float(df["close"].iloc[-1])
        except Exception as e2:
            raise RuntimeError(f"no usable crypto price for {symbol}: {e2}")

    # ── Trade execution ───────────────────────────────────────────────────────
    def attach_stop_target(
        self, symbol: str, qty: float, stop: float, target: float
    ) -> tuple[bool, bool]:
        """Attach a protective exit as a single OCO (one-cancels-other) order:
        a take-profit limit and a stop-loss that share the same shares. When
        either leg fills, Alpaca cancels the other — so both can coexist on one
        position (unlike two independent full-qty SELL orders, where the first
        reserves the shares and the second is rejected).

        OCO is atomic, so both legs attach together or neither does. Returns
        (stop_attached, target_attached).

        Submission is wrapped in safe_attach_oco, which retries once a stale
        sell order for this symbol is cancelled if Alpaca rejects the order
        with error 40310000 ("insufficient qty available for order").

        Alpaca rejects any GTC order on a fractional-qty position (error
        42210000, "fractional orders must be DAY orders") — a fractional
        buy's OCO/stop must use TimeInForce.DAY instead. That means the
        exit expires at the day's close; callers that run a position-repair
        pass (midday_review, market_open) are responsible for re-attaching
        it on days it's found missing.

        Alpaca separately rejects ANY non-SIMPLE order_class (OCO included)
        on a fractional qty outright (same error code 42210000, message
        "fractional orders must be simple orders" — a second, distinct
        restriction from the DAY-tif one above). A fractional position
        therefore can't get an atomic stop+target pair at all: submitting
        them as two independent SIMPLE sells would double-reserve the
        shares (the first order's shares aren't available to the second).
        So for fractional qty we attach ONLY the stop-loss as a SIMPLE
        order and skip the take-profit target entirely — protecting the
        downside is the priority, and giving up an automated take-profit
        exit (still handled by the position-review routines' own logic)
        beats the previous behavior, where the doomed OCO attempt failed
        outright, reported the position as fully unprotected, and callers'
        "flatten if stop not attached" guard immediately sold it back out -
        a same-second buy-then-sell that paid the spread twice for nothing."""
        is_fractional = qty != int(qty)
        if is_fractional and target is not None:
            log.info(
                f"  ↳ {symbol} qty {qty} is fractional — Alpaca disallows OCO for fractional "
                f"orders, attaching stop-only (no take-profit target) instead"
            )
            target = None

        # STOP_LIMIT_BUFFER_PCT: stop-limit fills cap slippage at 1.5% below
        # the stop price on gap-down opens. Trade-off: stop-limit orders do
        # not participate in the open/close auction (Alpaca docs). Accepted.
        stop_limit = max(round(stop * 0.985, 2), round(stop - 0.05, 2))
        tif = TimeInForce.DAY if is_fractional else TimeInForce.GTC

        def _submit():
            if target is not None:
                order = self.trade.submit_order(
                    LimitOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.SELL,
                        time_in_force=tif,
                        order_class=OrderClass.OCO,
                        take_profit=TakeProfitRequest(limit_price=target),
                        stop_loss=StopLossRequest(stop_price=stop, limit_price=stop_limit),
                    )
                )
                log.info(
                    f"  ↳ OCO attached ({tif.value}): stop @ ${stop:.2f} / target @ ${target:.2f} x{qty} [{symbol}]"
                )
            else:
                order = self.trade.submit_order(
                    StopLimitOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.SELL,
                        time_in_force=tif,
                        order_class=OrderClass.SIMPLE,
                        stop_price=stop,
                        limit_price=stop_limit,
                    )
                )
                log.info(
                    f"  ↳ SIMPLE STOP attached ({tif.value}, no cap): stop @ ${stop:.2f} x{qty} [{symbol}]"
                )
            return order

        try:
            submitted = safe_attach_oco(self, symbol, qty, stop, target, _submit)
        except Exception as e:
            log.error(f"  ↳ Order attach FAILED [{symbol}]: {e}")
            return False, False

        # submit_order() not raising only proves Alpaca accepted the HTTP
        # request — it does NOT prove the order survived Alpaca's async risk
        # checks (e.g. wash-trade / PDT / not-yet-settled-buy), which can
        # accept-then-reject an order moments after the synchronous response.
        # Verify by fetching the SPECIFIC order we just submitted (by ID),
        # not by fuzzy-matching against the open-orders list: scanning that
        # list for a "stop"-ish type raced Alpaca's own list-propagation lag
        # and repeatedly flattened live positions (TSL/NVDA 2026-08-11) even
        # though order history later confirmed the attach had succeeded —
        # get_order_by_id targets the exact resource instead of an
        # eventually-consistent aggregate view of it.
        order_id = getattr(submitted, "id", None)
        if not self._verify_stop_live(symbol, order_id=order_id):
            log.error(
                f"  ↳ Stop order attach for {symbol} reported success but no "
                f"live stop/OCO order was found on Alpaca afterward — "
                f"treating as NOT attached"
            )
            return False, False

        return True, target is not None

    def _verify_stop_live(
        self, symbol: str, order_id: str | None = None, max_attempts: int = 10, delay: float = 0.75
    ) -> bool:
        """Confirm the stop/OCO order just submitted is actually alive on
        Alpaca, rather than trusting that submit_order() not raising means
        the order exists (accept-then-reject on async risk checks is real).

        Preferred path: fetch the specific order_id via get_order_by_id and
        check its status directly — this targets the exact resource instead
        of Alpaca's aggregate open-orders list, which lags behind individual
        order state by an unpredictable amount right after a submit (worse
        right after a cancel-then-resubmit, as safe_attach_oco does on a
        40310000 retry). Falls back to the old list-scan when no order_id is
        available (e.g. called directly in a test/older code path).

        Budget is ~6.75s (10 attempts x 0.75s between polls). This budget
        was already widened once, from ~2.5s (6 x 0.5s), after the
        2026-08-05 12:07 ET incident flattened BAC/NKE/WFC on false-negative
        list-scan misses — and the *same* false-negative (not a timing
        shortfall; get_order_by_id found the order live on the very first
        poll once this fix shipped) still flattened TSL and NVDA on
        2026-08-11, which is why this now checks the order directly instead
        of re-widening the list-scan budget again."""
        terminal_statuses = {"canceled", "rejected", "expired", "done_for_day"}
        for attempt in range(max_attempts):
            if order_id:
                try:
                    o = self.trade.get_order_by_id(order_id)
                    if order_field(o, "status") not in terminal_statuses:
                        return True
                except Exception as e:
                    log.warning(
                        "  ↳ verify stop [%s]: get_order_by_id(%s) failed (attempt %d/%d): %s",
                        symbol,
                        order_id,
                        attempt + 1,
                        max_attempts,
                        e,
                    )
            else:
                try:
                    open_orders = self.get_open_orders()
                except Exception as e:
                    log.warning(
                        "  ↳ verify stop [%s]: could not list open orders (attempt %d/%d): %s",
                        symbol,
                        attempt + 1,
                        max_attempts,
                        e,
                    )
                    open_orders = None
                if open_orders:
                    for o in open_orders:
                        if o.symbol != symbol:
                            continue
                        if order_field(o, "side") != "sell":
                            continue
                        if "stop" in order_field(o, "type"):
                            return True
            if attempt < max_attempts - 1:
                time.sleep(delay)
        return False

    def affordable_budget(self, symbol: str) -> float:
        """Max dollars available for a NEW whole-share buy of symbol right now —
        the tighter of the MAX_POSITION_SIZE_PCT cap (net of any existing
        position in symbol) and available buying power. Callers use this to
        pre-filter screener candidates by price before attempting a buy() that
        would just get blocked — cheap enough to call per-candidate.
        """
        equity = self.portfolio_value()
        max_position_dollars = equity * MAX_POSITION_SIZE_PCT
        existing_pos = self.get_position(symbol)
        existing_value = (
            abs(float(existing_pos.market_value or 0)) if existing_pos is not None else 0.0
        )
        remaining_cap = max(0.0, max_position_dollars - existing_value)
        return min(remaining_cap, self.buying_power())

    # ── Dry-run short-circuit ────────────────────────────────────────────────
    def _dry_run_fill(
        self,
        *,
        symbol: str,
        qty: float | None,
        notional: float | None,
        ref_price: float,
        stop_loss_pct: float,
        take_profit_pct: float | None,
        strategy: str | None,
        spread_pct: float | None,
    ) -> dict:
        """DRY_RUN short-circuit for buy(): everything up to
        this point (spread gate, sizing guardrails) ran against real market
        data — this just replaces the real order submission + fill poll +
        stop/target attach with a simulated fill at ref_price, so no order
        ever reaches Alpaca. Logs what WOULD have been sent and still runs
        cost_tracker (signal_price == fill_price here, so realized slippage
        is 0 by construction — this validates the logging pipeline, not real
        execution cost, until DRY_RUN is turned off).
        """
        if qty is None:
            qty = round(notional / ref_price, 9)
        if notional is None:
            notional = round(qty * ref_price, 2)
        basis = ref_price
        stop = round(basis * (1 - stop_loss_pct), 2)
        target = round(basis * (1 + take_profit_pct), 2) if take_profit_pct is not None else None

        log.info(
            "[DRY_RUN] Would BUY %s qty=%s ($%.2f notional) @ ~$%.2f | SL=%s TP=%s "
            "-- no order submitted",
            symbol,
            qty,
            notional,
            basis,
            stop,
            target,
        )
        trade_logger.log_event(
            "dry_run_order",
            strategy or "unknown",
            symbol,
            side="buy",
            qty=qty,
            notional=notional,
            ref_price=basis,
            stop=stop,
            target=target,
            spread_pct=spread_pct,
        )
        if strategy is not None:
            cost_tracker.record_fill(
                strategy=strategy,
                symbol=symbol,
                side="buy",
                signal_price=ref_price,
                fill_price=basis,
                qty=qty,
                spread_pct=spread_pct,
            )

        return {
            "order": None,
            "qty": qty,
            "price": basis,
            "stop": stop,
            "target": target,
            "stop_attached": True,
            "target_attached": target is not None,
            "dry_run": True,
        }

    def buy(
        self,
        symbol: str,
        dollar_amount: float = None,
        shares: int = None,
        stop_loss_pct: float = STOP_LOSS_PCT,
        take_profit_pct: float = TAKE_PROFIT_PCT,
        strategy: str | None = None,
    ) -> dict:
        """
        Simple market BUY with hard position-sizing guardrails, then attach a
        protective OCO exit (stop + target) priced off the REAL fill price.

        Guardrails enforced before every order:
        0. Bid-ask spread must not exceed MAX_SPREAD_PCT of price (see
           check_spread) — hard block, applies uniformly across all
           strategies that route through buy().
        1. Position count must be below MAX_OPEN_POSITIONS (new symbols only).
        2. Order value is clamped to equity * MAX_POSITION_SIZE_PCT, accounting
           for any existing position in the same symbol.
        3. If even 1 whole share doesn't fit the remaining cap/cash, the
           trade is skipped rather than falling back to a fractional-share
           buy — Alpaca can only attach a DAY-tif stop to a fractional
           position (GTC is rejected), so it can never carry durable
           protection.

        Pass either dollar_amount OR shares. `strategy` (e.g. "breakout",
        "meanrev") is optional and only used to attribute cost-tracking
        (slippage) logging — omitting it just means that fill isn't logged
        to cost_tracker, everything else behaves the same.

        Returns qty, price (fill basis), stop, target, and stop_attached /
        target_attached bools.
        """
        # ── Guardrail 0: hard pre-trade spread gate ──────────────────────────
        spread_check = self.check_spread(symbol)
        if not spread_check.get("ok"):
            log.warning(
                "BUY %s BLOCKED — spread gate: %s (spread=%s)",
                symbol,
                spread_check.get("reason"),
                spread_check.get("spread_pct"),
            )
            return {
                "blocked": True,
                "reason": spread_check.get("reason", "spread_check_failed"),
                "spread_pct": spread_check.get("spread_pct"),
            }

        ref_price = self.get_price(symbol)
        equity = self.portfolio_value()
        max_position_dollars = equity * MAX_POSITION_SIZE_PCT

        # ── Guardrail 1: enforce MAX_OPEN_POSITIONS ──────────────────────────
        existing_pos = self.get_position(symbol)
        if existing_pos is None and self.position_count() >= MAX_OPEN_POSITIONS:
            log.warning(
                "BUY %s BLOCKED — already at %d/%d open positions",
                symbol,
                self.position_count(),
                MAX_OPEN_POSITIONS,
            )
            return {"blocked": True, "reason": "max_open_positions"}

        # ── Guardrail 2: clamp order to per-position cap ─────────────────────
        existing_value = 0.0
        if existing_pos is not None:
            existing_value = abs(float(existing_pos.market_value or 0))
        remaining_cap = max(0.0, max_position_dollars - existing_value)

        if remaining_cap <= 0:
            log.warning(
                "BUY %s BLOCKED — existing position $%.0f already at/above %.1f%% cap ($%.0f)",
                symbol,
                existing_value,
                MAX_POSITION_SIZE_PCT * 100,
                max_position_dollars,
            )
            return {"blocked": True, "reason": "position_size_cap"}

        MIN_ORDER_PRICE = 1.00  # reject sub-dollar stocks to prevent unrealistic share counts

        # ── PRIMARY: risk-parity sizing ──────────────────────────────────────────
        # Size = equity × RISK_PCT / stop_pct  →  each position risks RISK_PCT of equity
        # at the defined stop distance. SIZE_PCT acts as a hard ceiling on notional.
        if shares is not None:
            qty = shares
        elif ref_price >= MIN_ORDER_PRICE:
            risk_qty = max(1, int((equity * RISK_PCT) / (ref_price * stop_loss_pct)))
            # SIZE_PCT ceiling: prevent single-name from exceeding MAX_POSITION_SIZE_PCT
            size_qty = max(1, int(remaining_cap / ref_price))
            qty = min(risk_qty, size_qty)
            if dollar_amount is not None:
                # Caller's intended notional (e.g. a strategy's *_SIZE_PCT) is an
                # additional ceiling on top of risk-parity/MAX_POSITION_SIZE_PCT —
                # it only tightens sizing, never loosens the other guardrails.
                dollar_qty = max(1, int(dollar_amount / ref_price))
                qty = min(qty, dollar_qty)
        else:
            qty = 0

        if qty < 1:
            log.warning(
                "BUY %s BLOCKED — ref_price $%.4f below $%.2f minimum",
                symbol,
                ref_price,
                MIN_ORDER_PRICE,
            )
            return {"blocked": True, "reason": "min_price"}

        order_value = qty * ref_price
        if order_value > remaining_cap:
            clamped_qty = max(1, int(remaining_cap / ref_price))
            log.warning(
                "BUY %s CLAMPED — requested %d shares ($%.0f) exceeds "
                "%.1f%% cap; reduced to %d shares ($%.0f)",
                symbol,
                qty,
                order_value,
                MAX_POSITION_SIZE_PCT * 100,
                clamped_qty,
                clamped_qty * ref_price,
            )
            qty = clamped_qty

        if qty < 1:
            log.warning("BUY %s BLOCKED — clamped qty to 0", symbol)
            return {"blocked": True, "reason": "position_size_cap"}

        # ── Guardrail 3: clamp order to available cash / buying power ────────
        # Guardrail 2 only bounds notional against equity (portfolio_value),
        # which includes the market value of existing positions. On an
        # already-invested account, equity can far exceed actual spendable
        # cash, so a qty sized off equity alone can exceed buying power and
        # get rejected — or draw on margin — at submission time.
        available_cash = self.buying_power()
        order_value = qty * ref_price
        if order_value > available_cash:
            cash_qty = max(0, int(available_cash / ref_price))
            log.warning(
                "BUY %s CLAMPED — requested %d shares ($%.0f) exceeds "
                "available buying power ($%.0f); reduced to %d shares ($%.0f)",
                symbol,
                qty,
                order_value,
                available_cash,
                cash_qty,
                cash_qty * ref_price,
            )
            qty = cash_qty

        if qty < 1:
            # Fractional-share buys are disabled outright. Alpaca rejects GTC
            # on a fractional position (error 42210000) — only a DAY-tif stop
            # can attach, so protection lapses at that day's close and stays
            # off until a repair pass (midday_review/market_open) happens to
            # re-attach it. That gap is exactly what left MSFT sitting on the
            # exchange with zero live stop for days before it was caught and
            # closed manually (2026-08-15). A position that can only ever get
            # temporary protection isn't worth opening — skip the trade.
            log.warning(
                "BUY %s BLOCKED — cannot afford 1 whole share @ $%.2f "
                "(remaining cap $%.2f, buying power $%.2f); fractional buys are disabled",
                symbol,
                ref_price,
                remaining_cap,
                available_cash,
            )
            return {"blocked": True, "reason": "insufficient_cash"}

        if DRY_RUN:
            return self._dry_run_fill(
                symbol=symbol,
                qty=qty,
                notional=None,
                ref_price=ref_price,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                strategy=strategy,
                spread_pct=spread_check.get("spread_pct"),
            )

        # 1. Simple market BUY
        order = self.trade.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        )
        log.info(f"BUY {symbol} x{qty} (market) submitted [{str(order.id)[:8]}]")

        # 2. Poll for the actual fill price (up to ~5s); exit early if market closed
        market_open = self.is_market_open()
        fill_price = None
        filled_qty = qty
        for i in range(10):
            if not market_open and i > 0:
                log.warning("Market closed — aborting fill poll for %s", symbol)
                break
            try:
                o = self.trade.get_order_by_id(order.id)
            except Exception as e:
                log.warning("poll order %s failed: %s", symbol, e)
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
            log.warning(
                f"{symbol} not filled within 5s — using reference ${ref_price:.2f} for stop/target"
            )
        stop = round(basis * (1 - stop_loss_pct), 2)
        target = round(basis * (1 + take_profit_pct), 2) if take_profit_pct is not None else None

        # 4. Attach protective stop-loss + take-profit (each its own try/except)
        stop_attached, target_attached = self.attach_stop_target(symbol, filled_qty, stop, target)
        log.info(
            f"BUY {symbol} x{filled_qty} @ ${basis:.2f} | SL={stop} "
            f"TP={'None (no cap)' if target is None else target} "
            f"| stop_attached={stop_attached} target_attached={target_attached}"
        )

        if strategy is not None and fill_price is not None:
            cost_tracker.record_fill(
                strategy=strategy,
                symbol=symbol,
                side="buy",
                signal_price=ref_price,
                fill_price=fill_price,
                qty=filled_qty,
                spread_pct=spread_check.get("spread_pct"),
            )

        return {
            "order": order,
            "qty": filled_qty,
            "price": basis,
            "stop": stop,
            "target": target,
            "stop_attached": stop_attached,
            "target_attached": target_attached,
        }

    def sell(self, symbol: str, qty: float = None) -> dict:
        """Market sell. qty=None → close entire position."""
        if qty is None:
            pos = self.get_position(symbol)
            if not pos:
                log.warning(f"No position in {symbol}")
                return {}
            # Keep fractional precision — int() truncation here would submit
            # qty=0 for any position under 1 share and silently no-op the
            # close (Alpaca DAY orders already support fractional qty).
            qty = float(pos.qty)

        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self.trade.submit_order(req)
        log.info(f"SELL {symbol} x{qty}")
        return {"order": order, "qty": qty}

    def buy_simple(
        self,
        symbol: str,
        dollar_amount: float,
        strategy: str | None = None,
    ) -> dict:
        """
        Simple market BUY with NO protective stop/target attached.

        For strategies whose exit logic is fundamentals/profit-target driven
        rather than price-distance driven (Buffett Value is the first: no
        hard percentage stop-loss by design, see
        skills/buffett-value/scripts/sell.py) — buy() is unsuitable because
        its risk-parity sizing formula and unconditional attach_stop_target()
        call both assume a stop distance exists.

        Applies the same guardrails as buy() (spread gate, MAX_OPEN_POSITIONS,
        per-position cap, buying-power clamp) but sizes purely off
        dollar_amount / ref_price — no risk-parity math, since there is no
        stop distance to size against. Whole shares only (no fractional
        fallback): acceptable for a low-frequency, concentrated strategy
        screening large-cap names.

        Returns {qty, price, blocked?}. Never attaches a stop or target.
        """
        spread_check = self.check_spread(symbol)
        if not spread_check.get("ok"):
            log.warning(
                "BUY_SIMPLE %s BLOCKED — spread gate: %s (spread=%s)",
                symbol,
                spread_check.get("reason"),
                spread_check.get("spread_pct"),
            )
            return {
                "blocked": True,
                "reason": spread_check.get("reason", "spread_check_failed"),
                "spread_pct": spread_check.get("spread_pct"),
            }

        ref_price = self.get_price(symbol)
        equity = self.portfolio_value()
        max_position_dollars = equity * MAX_POSITION_SIZE_PCT

        existing_pos = self.get_position(symbol)
        if existing_pos is None and self.position_count() >= MAX_OPEN_POSITIONS:
            log.warning(
                "BUY_SIMPLE %s BLOCKED — already at %d/%d open positions",
                symbol,
                self.position_count(),
                MAX_OPEN_POSITIONS,
            )
            return {"blocked": True, "reason": "max_open_positions"}

        existing_value = 0.0
        if existing_pos is not None:
            existing_value = abs(float(existing_pos.market_value or 0))
        remaining_cap = max(0.0, max_position_dollars - existing_value)

        if remaining_cap <= 0:
            log.warning(
                "BUY_SIMPLE %s BLOCKED — existing position $%.0f already at/above %.1f%% cap ($%.0f)",
                symbol,
                existing_value,
                MAX_POSITION_SIZE_PCT * 100,
                max_position_dollars,
            )
            return {"blocked": True, "reason": "position_size_cap"}

        MIN_ORDER_PRICE = 1.00
        if ref_price < MIN_ORDER_PRICE:
            log.warning(
                "BUY_SIMPLE %s BLOCKED — ref_price $%.4f below $%.2f minimum",
                symbol,
                ref_price,
                MIN_ORDER_PRICE,
            )
            return {"blocked": True, "reason": "min_price"}

        notional_cap = min(dollar_amount, remaining_cap)
        qty = max(0, int(notional_cap / ref_price))

        if qty < 1:
            log.warning("BUY_SIMPLE %s BLOCKED — sizing produced 0 shares", symbol)
            return {"blocked": True, "reason": "position_size_cap"}

        available_cash = self.buying_power()
        order_value = qty * ref_price
        if order_value > available_cash:
            qty = max(0, int(available_cash / ref_price))
            if qty < 1:
                log.warning(
                    "BUY_SIMPLE %s BLOCKED — cannot afford 1 whole share @ $%.2f "
                    "(buying power $%.2f)",
                    symbol,
                    ref_price,
                    available_cash,
                )
                return {"blocked": True, "reason": "insufficient_cash"}

        if DRY_RUN:
            notional = round(qty * ref_price, 2)
            log.info(
                "[DRY_RUN] Would BUY_SIMPLE %s qty=%s ($%.2f notional) @ ~$%.2f -- no order submitted",
                symbol, qty, notional, ref_price,
            )
            trade_logger.log_event(
                "dry_run_order", strategy or "unknown", symbol,
                side="buy", qty=qty, notional=notional, ref_price=ref_price,
                spread_pct=spread_check.get("spread_pct"),
            )
            if strategy is not None:
                cost_tracker.record_fill(
                    strategy=strategy, symbol=symbol, side="buy",
                    signal_price=ref_price, fill_price=ref_price, qty=qty,
                    spread_pct=spread_check.get("spread_pct"),
                )
            return {"order": None, "qty": qty, "price": ref_price, "dry_run": True}

        order = self.trade.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        )
        log.info(f"BUY_SIMPLE {symbol} x{qty} (market, no stop) submitted [{str(order.id)[:8]}]")

        market_open = self.is_market_open()
        fill_price = None
        filled_qty = qty
        for i in range(10):
            if not market_open and i > 0:
                log.warning("Market closed — aborting fill poll for %s", symbol)
                break
            try:
                o = self.trade.get_order_by_id(order.id)
            except Exception as e:
                log.warning("poll order %s failed: %s", symbol, e)
                break
            if o.filled_avg_price:
                fill_price = float(o.filled_avg_price)
                if o.filled_qty:
                    filled_qty = int(float(o.filled_qty))
                break
            time.sleep(0.5)

        basis = fill_price if fill_price else ref_price
        # Matches buy()'s guard (core/broker.py ~line 780): only log a real
        # fill to cost_tracker, not a synthetic zero-slippage entry for an
        # order that never confirmed filled within the poll window.
        if strategy is not None and fill_price is not None:
            cost_tracker.record_fill(
                strategy=strategy, symbol=symbol, side="buy",
                signal_price=ref_price, fill_price=basis, qty=filled_qty,
                spread_pct=spread_check.get("spread_pct"),
            )
        log.info(f"BUY_SIMPLE {symbol} x{filled_qty} @ ${basis:.2f} -- no stop attached")
        return {"order": order, "qty": filled_qty, "price": basis}

    def sell_limit(self, symbol: str, qty: float, limit_price: float) -> dict:
        """
        Simple (non-bracket) limit SELL — never OCO. For strategies without a
        stop leg to pair against (Buffett Value's exits are single-leg:
        profit target / thesis break / better opportunity — see
        skills/buffett-value/scripts/sell.py:build_sell_order).

        Polls briefly for a fill (mirrors buy()/buy_simple()) since the
        limit price is typically set just inside the current quote and so
        fills almost immediately when marketable. Callers MUST check the
        returned "filled" flag before treating this as a completed exit —
        an unconfirmed order is still resting at the broker (or already
        expired, since it's a DAY order) and the caller should NOT log
        success, alert, or stop tracking the position.
        """
        if DRY_RUN:
            log.info(
                "[DRY_RUN] Would SELL_LIMIT %s x%s @ $%.2f -- no order submitted",
                symbol, qty, limit_price,
            )
            return {
                "order": None, "qty": qty, "limit_price": limit_price, "dry_run": True,
                "filled": True, "filled_qty": qty, "filled_avg_price": limit_price,
            }

        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.SIMPLE,
            limit_price=limit_price,
        )
        order = self.trade.submit_order(req)
        log.info(f"SELL_LIMIT {symbol} x{qty} @ ${limit_price:.2f} (simple, no bracket) submitted [{str(order.id)[:8]}]")

        filled_qty = None
        filled_avg_price = None
        for i in range(10):
            try:
                o = self.trade.get_order_by_id(order.id)
            except Exception as e:
                log.warning("poll order %s failed: %s", symbol, e)
                break
            if o.filled_avg_price:
                filled_avg_price = float(o.filled_avg_price)
                if o.filled_qty:
                    filled_qty = float(o.filled_qty)
                break
            time.sleep(0.5)

        filled = filled_avg_price is not None
        if filled:
            log.info(f"SELL_LIMIT {symbol} x{filled_qty} @ ${filled_avg_price:.2f} CONFIRMED FILLED")
        else:
            log.warning(f"SELL_LIMIT {symbol} not confirmed filled within 5s -- still resting at ${limit_price:.2f}")

        return {
            "order": order,
            "qty": qty,
            "limit_price": limit_price,
            "filled": filled,
            "filled_qty": filled_qty,
            "filled_avg_price": filled_avg_price,
        }

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
          2. OCO child stop-loss leg (type=stop, returned as separate order) — replace.
        Returns True only if the order was actually replaced on Alpaca.

        Note: Alpaca doesn't expose stop_price on the OCO parent — the stop-loss
        is a child order returned as type=stop. We match by type instead."""
        try:
            open_orders = self.get_open_orders()
            log.info(f"tighten_stop: {len(open_orders)} open orders total")
            # Log all orders for this symbol for debugging
            for o in open_orders:
                if o.symbol == symbol:
                    log.info(
                        f"  Order: id={o.id} type={o.type} side={o.side} "
                        f"order_class={getattr(o, 'order_class', 'n/a')} "
                        f"stop_price={getattr(o, 'stop_price', 'n/a')} "
                        f"limit_price={getattr(o, 'limit_price', 'n/a')}"
                    )

            # Match stop orders for this symbol (handles both standalone stops
            # and OCO child stop-loss legs, which Alpaca returns as separate rows)
            candidates = []
            for o in open_orders:
                if o.symbol != symbol:
                    continue
                # order_field: str(enum) is 'OrderSide.SELL' — the old
                # str().lower() side check matched nothing, so tighten_stop
                # always reported "no open stop order".
                if order_field(o, "side") != "sell":
                    continue
                if "stop" not in order_field(o, "type"):
                    continue
                candidates.append(o)

            if not candidates:
                log.warning("tighten_stop: no open stop order for %s", symbol)
                return False
            order = candidates[0]
            old_stop = getattr(order, "stop_price", None)
            old_label = (
                f"${old_stop:.2f}" if isinstance(old_stop, (int, float)) else str(old_stop or "?")
            )
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
        """Try both API signatures regardless of exception type."""
        if GetPortfolioHistoryRequest is None:
            log.warning("GetPortfolioHistoryRequest not available in this alpaca-py version")
            return None
        req = GetPortfolioHistoryRequest(period=period, timeframe="1D")
        signatures = [
            [("history_filter", req)],
            [("positional", req)],
        ]
        for sig_args in signatures:
            try:
                if sig_args[0][0] == "history_filter":
                    return self.trade.get_portfolio_history(history_filter=req)
                else:
                    return self.trade.get_portfolio_history(req)
            except TypeError:
                continue
            except Exception as e:
                log.warning("get_portfolio_history: %s", e)
                raise
        log.error("get_portfolio_history: both signatures exhausted")
        return None

    # ── Market status ─────────────────────────────────────────────────────────
    def is_market_open(self) -> bool:
        clock = self.trade.get_clock()
        return clock.is_open

    def next_open(self):
        return self.trade.get_clock().next_open

    def next_close(self):
        return self.trade.get_clock().next_close

    # ── Options trading (CSP / Cash-Secured Puts) ─────────────────────────────────
    def sell_csp(
        self,
        symbol: str,
        strike: float,
        expiration: str,  # YYYY-MM-DD
        premium: float = None,
        qty: int = 1,
    ) -> dict:
        """
        Sell a Cash-Secured Put (CSP) — sell_to_open a put option.

        symbol: underlying stock (e.g. "INTC")
        strike: strike price (e.g. 90.0)
        expiration: ISO date string (YYYY-MM-DD)
        premium: limit price in dollars (if None, attempt MARKET)
        qty: number of contracts (default 1 = 100 shares collateral)

        Returns order dict with contract details and filled price.

        NOTE: Account must have options_level >= 1 and sufficient buying power.
        """
        try:
            from alpaca.trading.enums import (
                OrderSide as OptSide,
            )
            from alpaca.trading.enums import (
                OrderType as OptType,
            )
            from alpaca.trading.enums import (
                TimeInForce as OptTIF,
            )
            from alpaca.trading.requests import (
                GetOptionContractsRequest,
                OptionTradeRequest,
            )
        except ImportError:
            log.error("Alpaca options not supported — upgrade alpaca-py: pip install alpaca-py")
            return {"blocked": True, "reason": "no_options_support"}

        # Build OCC symbol: format = ROOT + DATE + C/P + STRIKE
        # Example: INTC 20260725 $90.00 PUT → "INTC20260725P90"
        strike_int = int(strike)
        strike_dec = int((strike - strike_int) * 1000)
        if strike < 1:
            strike_dec = int((strike - strike_int) * 10000)
            occ_symbol = f"{symbol.upper()}{expiration.replace('-', '')}{strike_dec:08d}P"
        elif strike < 1000:
            occ_symbol = f"{symbol.upper()}{expiration.replace('-', '')}P{int(strike * 1000):08d}"
        else:
            occ_symbol = f"{symbol.upper()}{expiration.replace('-', '')}P{int(strike * 100):05d}0"

        log.info(f"CSP: searching contract {occ_symbol} for {symbol} strike=${strike}")

        # Look up the contract
        try:
            req = GetOptionContractsRequest(
                underlying_symbols=[symbol.upper()],
                expiration_date_gte=expiration,
                expiration_date_lte=expiration,
                _fcc_arg_type="put",
                strike_price_gte=float(strike) * 0.9,
                strike_price_lte=float(strike) * 1.1,
                status="active",
                limit=10,
            )
            contracts = list(self.trade.get_option_contracts(req))
            log.info(f"  Found {len(contracts)} contracts")
        except Exception as e:
            log.error(f"  Contract lookup failed: {e}")
            return {"blocked": True, "reason": f"contract_lookup_failed: {e}"}

        if not contracts:
            log.error(f"No contracts found for {occ_symbol}")
            return {"blocked": True, "reason": "no_contract_found"}

        # Pick the one matching our strike best
        target = float(strike)
        contract = min(contracts, key=lambda c: abs(float(c.strike_price or 0) - target))
        occ_symbol = contract.symbol
        log.info(f"  Using contract: {occ_symbol} strike=${contract.strike_price}")

        # Check buying power
        acct = self.get_account()
        options_bp = float(acct.options_buying_power or 0)
        if options_bp < strike * 100:
            log.warning(
                f"  Insufficient options BP: ${options_bp:.2f} < ${strike * 100:.2f} required"
            )
            return {"blocked": True, "reason": "insufficient_buying_power"}

        # Submit the order
        try:
            if premium:
                order = self.trade.submit_option_order(
                    OptionTradeRequest(
                        symbol=occ_symbol,
                        qty=str(qty),
                        side=OptSide.SELL_TO_OPEN,
                        type=OptType.LIMIT,
                        limit_price=str(premium),
                        time_in_force=OptTIF.GTC,
                    )
                )
                log.info(f"  LIMIT CSP submitted: {occ_symbol} x{qty} @ ${premium}")
            else:
                order = self.trade.submit_option_order(
                    OptionTradeRequest(
                        symbol=occ_symbol,
                        qty=str(qty),
                        side=OptSide.SELL_TO_OPEN,
                        type=OptType.MARKET,
                        time_in_force=OptTIF.GTC,
                    )
                )
                log.info(f"  MARKET CSP submitted: {occ_symbol} x{qty}")

            # Poll for fill
            filled_price = None
            for _ in range(20):
                try:
                    o = self.trade.get_order_by_id(order.id)
                    if o and o.filled_avg_price:
                        filled_price = float(o.filled_avg_price)
                        break
                except Exception:
                    pass
                time.sleep(1)

            collateral = target * 100 * qty
            log.info(
                f"  CSP filled: ${filled_price} | premium=${filled_price * 100:.2f}"
                if filled_price
                else f"  CSP pending: {order.id}"
            )

            return {
                "order": order,
                "contract": occ_symbol,
                "symbol": symbol,
                "strike": target,
                "expiration": expiration,
                "qty": qty,
                "type": "csp_sell",
                "side": "sell_to_open",
                "fill_price": filled_price,
                "premium_collected": filled_price * 100 if filled_price else 0,
                "collateral": collateral,
                "status": o.status.value if o else "pending",
            }

        except Exception as e:
            log.error(f"CSP order failed: {e}")
            return {"blocked": True, "reason": str(e)}

    def get_put_contracts(self, symbol: str, expiration: str, max_strike: float = None) -> list:
        """Fetch available put option contracts for a symbol on a given expiry date."""
        try:
            from alpaca.trading.requests import GetOptionContractsRequest

            kwargs = dict(
                underlying_symbols=[symbol.upper()],
                expiration_date_gte=expiration,
                expiration_date_lte=expiration,
                _fcc_arg_type="put",
                status="active",
                limit=50,
            )
            if max_strike is not None:
                kwargs["strike_price_lte"] = float(max_strike)
            req = GetOptionContractsRequest(**kwargs)
            return list(self.trade.get_option_contracts(req))
        except Exception as e:
            log.warning("Option chain %s/%s: %s", symbol, expiration, e)
            return []

    def close_option(self, contract_symbol: str, qty: int = 1) -> dict:
        """Buy to close an existing short option position."""
        try:
            from alpaca.trading.enums import OrderSide, OrderType, TimeInForce

            order = self.trade.submit_option_order(
                symbol=contract_symbol,
                qty=str(qty),
                side=OrderSide.BUY_TO_CLOSE,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )
            log.info(f"BTC option {contract_symbol} x{qty} order={order.id}")
            return {"order": order, "contract": contract_symbol}
        except Exception as e:
            log.error(f"Close option failed: {e}")
            return {"blocked": True, "reason": str(e)}

    def get_options_positions(self) -> list:
        """Return all open option positions."""
        try:
            return self.trade.get_all_positions()
        except Exception:
            return []

    def options_level(self) -> int:
        """Return account options trading level (0=disabled, 1=CSP/CC only, 2=long, 3=strategies)."""
        acct = self.get_account()
        return int(getattr(acct, "options_trading_level", 0) or 0)
