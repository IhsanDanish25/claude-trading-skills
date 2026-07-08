from __future__ import annotations
"""
MARKET-OPEN ROUTINE — 9:30 AM ET, Mon-Fri
FULL SKILLS, ALPACA-ONLY (no FMP = no rate limits).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import datetime
import pytz

from core import logger, config
from core.broker import BrokerClient
from core.screener import screen
from core.notifier import send_trade_alert
from core.edge import circuit_breaker_tripped
from core import composite
from core.universe import build_universe
from circuit_breaker import CircuitBreaker, TradingHalted
from regime_gate import classify
from core.earnings_screener import screen_earnings
from core.pead_tracker import add_position as pead_track
from core.spy_base import rebalance_to_spy, free_cash_for_pead, log_status as spy_log
from core import trade_logger

log = logger.setup("market_open")
ET  = pytz.timezone("America/New_York")

# Strategy screeners — fail gracefully if FMP unavailable, but log the real
# cause so an import bug doesn't masquerade as "FMP unavailable" forever.
try:
    from core.meanrev_screener import screen as screen_meanrev
except Exception as e:
    log.error("MeanRev screener import failed: %s", e)
    screen_meanrev = None
try:
    from core.insider_screener import screen as screen_insider
except Exception as e:
    log.error("Insider screener import failed: %s", e)
    screen_insider = None
try:
    from core.squeeze_screener import screen as screen_squeeze
except Exception as e:
    log.error("Squeeze screener import failed: %s", e)
    screen_squeeze = None
try:
    from core.breakout_screener import screen as screen_breakout
except Exception as e:
    log.error("Breakout screener import failed: %s", e)
    screen_breakout = None
try:
    from core.earnings_momentum_screener import screen as screen_earnmom
except Exception as e:
    log.error("EarnMom screener import failed: %s", e)
    screen_earnmom = None


def _build_breaker(broker: BrokerClient, day_start_equity: float) -> CircuitBreaker:
    """day_start_equity: pre-market open equity from market_open.py's load_day_start_value(),
    NOT the broker's live equity. Prevents tick-time drift from corrupting the daily-loss
    baseline. See circuit_breaker.py for the bug fixed here."""
    return CircuitBreaker(
        get_account=broker.get_account,
        get_positions=broker.get_positions,
        max_open_positions=config.MAX_OPEN_POSITIONS,
        max_position_pct=config.MAX_POSITION_SIZE_PCT,
        max_daily_loss=config.CIRCUIT_BREAKER_PCT,
        day_start_equity=day_start_equity,
    )

DAY_START_PATH = os.path.join(config.STATE_DIR, "day_start_value.json")
TODAY_BOUGHT_PATH = os.path.join(config.STATE_DIR, "today_bought.json")
TRADE_LOG_PATH = os.path.join(config.STATE_DIR, "trade_log.jsonl")
MAX_BUYS       = 3


def _append_trade_log(entry: dict) -> None:
    """Record one order to BOTH sinks: state/trade_log.jsonl (local, kept for
    _reconcile_closed_trades) AND Axiom (durable, survives Railway redeploys).
    Delegates to trade_logger so the jsonl format/path is unchanged — reconcile
    still finds side=="buy" rows exactly as before. Non-blocking on error."""
    trade_logger.append_record(entry)


def _reconcile_closed_trades(broker) -> int:
    """For each buy row in trade_log.jsonl with pnl_pct=null, look up the matching
    closed SELL order on Alpaca. If found, fill exit_price/exit_date/pnl_pct in
    place and rewrite the JSONL. Returns the number of rows reconciled today.

    Called at the top of run() so trade_log.jsonl reflects realized exits from
    yesterday and earlier before anything else reads it.
    """
    if not os.path.exists(TRADE_LOG_PATH):
        return 0
    try:
        with open(TRADE_LOG_PATH) as f:
            lines = f.readlines()
    except OSError as e:
        log.warning(f"trade_log read failed during reconcile: {e}")
        return 0

    pending = []
    parsed = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            parsed.append({"_raw": line.rstrip("\n")})
            continue
        if rec.get("side") == "buy" and rec.get("pnl_pct") is None and rec.get("symbol"):
            pending.append(rec)
        parsed.append(rec)

    if not pending:
        return 0

    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus, OrderSide

    symbols = sorted({r["symbol"] for r in pending})
    try:
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            side=OrderSide.SELL,
            symbols=symbols,
            limit=200,
        )
        closed_orders = broker.trade.get_orders(filter=req) or []
    except Exception as e:
        log.warning(f"closed-order fetch failed (non-blocking): {e}")
        return 0

    last_sell_by_symbol: dict[str, dict] = {}
    for o in closed_orders:
        sym = getattr(o, "symbol", None)
        filled_at = getattr(o, "filled_at", None) or getattr(o, "submitted_at", None)
        avg = getattr(o, "filled_avg_price", None)
        if not sym or avg is None:
            continue
        cur = last_sell_by_symbol.get(sym)
        if cur is None or (filled_at and (cur.get("_ts") or "") < str(filled_at)):
            last_sell_by_symbol[sym] = {
                "_ts": str(filled_at) if filled_at else "",
                "exit_price": float(avg),
                "exit_date": str(filled_at)[:10] if filled_at else None,
            }

    reconciled = 0
    for rec in pending:
        sym = rec["symbol"]
        exit_info = last_sell_by_symbol.get(sym)
        if not exit_info:
            continue
        entry_price = rec.get("price")
        if not entry_price:
            continue
        pnl_pct = (exit_info["exit_price"] / float(entry_price) - 1.0) * 100.0
        rec["exit_price"] = exit_info["exit_price"]
        rec["exit_date"]  = exit_info["exit_date"]
        rec["pnl_pct"]    = round(pnl_pct, 4)
        reconciled += 1

    if reconciled > 0:
        try:
            tmp_path = TRADE_LOG_PATH + ".tmp"
            with open(tmp_path, "w") as f:
                for rec in parsed:
                    if "_raw" in rec:
                        f.write(rec["_raw"] + "\n")
                    else:
                        f.write(json.dumps(rec) + "\n")
            os.replace(tmp_path, TRADE_LOG_PATH)
        except OSError as e:
            log.warning(f"trade_log rewrite failed: {e}")

    return reconciled


def _load_today_bought() -> set:
    try:
        today = datetime.datetime.now(ET).date().isoformat()
        with open(TODAY_BOUGHT_PATH) as f:
            data = json.load(f)
        if data.get("date") != today:
            return set()
        return set(data.get("symbols", []))
    except (FileNotFoundError, ValueError, KeyError):
        return set()


def _mark_bought(symbol: str, result: dict) -> None:
    try:
        today = datetime.datetime.now(ET).date().isoformat()
        bought = _load_today_bought()
        bought.add(symbol)
        with open(TODAY_BOUGHT_PATH, "w") as f:
            json.dump({
                "date": today,
                "symbols": sorted(bought),
                "orders": [
                    {"symbol": s, "order_id": None} for s in sorted(bought)
                ],
            }, f, indent=2)
    except Exception as e:
        log.warning(f"Failed to persist today_bought state: {e}")


def load_day_start_value(current_pv: float) -> float:
    today = datetime.datetime.now(ET).date().isoformat()
    try:
        with open(DAY_START_PATH) as f:
            data = json.load(f)
        if data.get("date") == today and data.get("value"):
            return float(data["value"])
    except (FileNotFoundError, ValueError, KeyError):
        pass
    with open(DAY_START_PATH, "w") as f:
        json.dump({"date": today, "value": current_pv}, f)
    log.info(f"Recorded day-start portfolio value: ${current_pv:,.2f}")
    return current_pv


def is_entry_window():
    now = datetime.datetime.now(ET)
    open_t   = now.replace(hour=9, minute=30, second=0, microsecond=0)
    earliest = open_t + datetime.timedelta(minutes=config.ENTRY_DELAY_MIN)
    close_t  = now.replace(hour=15, minute=45, second=0, microsecond=0)
    if now < earliest:
        return False, f"too early — wait until {earliest.strftime('%H:%M')} ET"
    if now > close_t:
        return False, "too late — within 15min of close"
    return True, "entry window open"


def _run_pead(broker, cb, pv, slots, held, already_bought_today):
    """PEAD strategy: buy stocks with big earnings surprises, hold 60 days,
    -15% disaster stop, no take-profit. Time-exit handled by market_close."""
    log.info("PEAD: screening S&P 500 for earnings beats...")
    candidates = screen_earnings(
        lookback_days=config.PEAD_LOOKBACK_DAYS,
        min_surprise_pct=config.PEAD_MIN_SURPRISE_PCT,
        min_price=config.PEAD_MIN_PRICE,
        min_avg_volume=config.PEAD_MIN_AVG_VOLUME,
    )
    log.info(f"PEAD: {len(candidates)} candidates with surprise >= {config.PEAD_MIN_SURPRISE_PCT}%")

    if not candidates:
        log.info("PEAD: no earnings beats — done")
        return

    for c in candidates:
        log.info(f"  • {c['symbol']} surprise={c['surprise_pct']:+.1f}% "
                 f"EPS={c.get('actual_eps')}/{c.get('estimated_eps')} "
                 f"reported={c['report_date']} price=${c.get('price', 0):.2f}")
        trade_logger.log_event(
            "signal_detected", "pead", c["symbol"],
            surprise_pct=c["surprise_pct"], report_date=c["report_date"],
            price=c.get("price", 0), actual_eps=c.get("actual_eps"),
            estimated_eps=c.get("estimated_eps"),
        )

    buys_taken = 0
    for c in candidates[:slots[0]]:
        sym = c["symbol"]
        surprise = c["surprise_pct"]
        price = c.get("price", 0)

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event("order_skipped", "pead", sym,
                                   gate="already_held", reason="already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event("order_skipped", "pead", sym,
                                   gate="idempotency", reason="already bought today")
            continue

        size_pct = config.PEAD_SIZE_PCT
        amount = pv * size_pct

        log.info(f"PEAD BUY {sym} | surprise={surprise:+.1f}% | "
                 f"size={size_pct*100:.0f}% | ${amount:,.0f}")
        try:
            # Free SPY cash if needed for this PEAD entry
            if not free_cash_for_pead(broker, amount):
                log.warning(f"✗ {sym} SKIP — cannot free ${amount:,.0f} from SPY base")
                trade_logger.log_event("gate_failed", "pead", sym,
                                       gate="free_cash", amount=round(amount, 2),
                                       reason="cannot free cash from SPY base")
                continue
            trade_logger.log_event("gate_passed", "pead", sym,
                                   gate="free_cash", amount=round(amount, 2))

            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event("gate_passed", "pead", sym,
                                       gate="circuit_breaker", amount=round(amount, 2))
            except TradingHalted as halt:
                log.warning(f"✗ {sym} blocked by circuit breaker: {halt}")
                trade_logger.log_event("gate_failed", "pead", sym,
                                       gate="circuit_breaker", reason=str(halt))
                continue

            # PEAD uses wide stop (-15%), NO take-profit (time exit at 60d)
            # Set take_profit very wide (99%) so OCO doesn't trigger early
            result = broker.buy(
                sym,
                dollar_amount=amount,
                stop_loss_pct=config.PEAD_STOP_PCT,
                take_profit_pct=None,  # no hard target (time-managed 60d exit)
            )
            if result.get("blocked"):
                log.warning(f"✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event("order_skipped", "pead", sym,
                                       gate="broker_buy", reason=result.get("reason"))
                continue
            if not result.get("stop_attached"):
                log.error(f"✗ {sym} bought but stop NOT attached — flattening")
                broker.sell(sym, qty=result["qty"])
                send_trade_alert(
                    action="FLATTEN", ticker=sym, shares=result["qty"],
                    price=result["price"],
                    reason="PEAD stop-loss attach failed — position rejected",
                )
                trade_logger.log_event("order_skipped", "pead", sym,
                                       gate="stop_attach", reason="stop-loss attach failed — flattened",
                                       qty=result["qty"], price=result["price"])
                continue

            log.info(f"✓ PEAD {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.PEAD_HOLD_DAYS}d)")
            trade_logger.log_event("order_placed", "pead", sym,
                                   qty=result["qty"], price=result["price"],
                                   stop=result["stop"], surprise_pct=surprise,
                                   amount=round(amount, 2), hold_days=config.PEAD_HOLD_DAYS)

            # Track for time-based exit
            pead_track(sym, result["price"], surprise, c["report_date"])

            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=None,
                reason=f"PEAD surprise={surprise:+.1f}% hold={config.PEAD_HOLD_DAYS}d",
            )
            _mark_bought(sym, result)
            _append_trade_log({
                "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                "symbol": sym,
                "side": "buy",
                "qty": result.get("qty"),
                "price": result.get("price"),
                "stop": result.get("stop"),
                "target": None,
                "strategy": "pead",
                "surprise_pct": surprise,
                "exit_date": None,
                "exit_price": None,
                "pnl_pct": None,
            })
            buys_taken += 1
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — PEAD stopping")
                break
        except Exception as e:
            log.error(f"✗ PEAD {sym} buy failed: {e}")

    log.info(f"PEAD complete | Buys taken: {buys_taken}")

    # Rebalance idle cash back into SPY
    spy_log(broker)
    spy_result = rebalance_to_spy(broker)
    if spy_result["action"] not in ("none", "disabled"):
        log.info(f"SPY base: {spy_result['action']} {spy_result.get('qty', 0)} shares")


# ─────────────────────────────────────────────────────────────────────────────
# Strategy runner stubs — one per strategy mode
# Each follows the same contract as _run_pead:
#   broker, cb, pv, slots, held, already_bought_today → None
# Each calls config.{STRATEGY}_SIZE_PCT, .{STRATEGY}_STOP_PCT, .{STRATEGY}_HOLD_DAYS
# ─────────────────────────────────────────────────────────────────────────────

def _run_meanrev(broker, cb, pv, slots, held, already_bought_today):
    """Mean Reversion: RSI<30 + Bollinger oversold + above SMA200. Hold ~14d."""
    if screen_meanrev is None:
        log.warning("MeanRev: screener not loaded — see import error above — skipping")
        return
    log.info("MeanRev: screening RSI < 30 + Bollinger Band oversold...")
    candidates = screen_meanrev()
    log.info(f"MeanRev: {len(candidates)} candidates")
    if not candidates:
        return

    for c in candidates:
        sym = c["symbol"]
        price = c["price"]
        size_pct = config.MEANREV_SIZE_PCT
        amount = pv * size_pct

        trade_logger.log_event(
            "signal_detected", "meanrev", sym,
            rsi=c["rsi"], bb_position=c["bb_position"],
            momentum_pct=c.get("momentum_pct", 0), price=price,
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event("order_skipped", "meanrev", sym,
                                   gate="already_held", reason="already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event("order_skipped", "meanrev", sym,
                                   gate="idempotency", reason="already bought today")
            continue

        log.info(f"MeanRev BUY {sym} | RSI={c['rsi']} BBpos={c['bb_position']:.0f}% "
                 f"momentum={c.get('momentum_pct', 0):+.1f}% | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash from SPY base")
                trade_logger.log_event("gate_failed", "meanrev", sym,
                                       gate="free_cash", amount=round(amount, 2),
                                       reason="cannot free cash from SPY base")
                continue
            trade_logger.log_event("gate_passed", "meanrev", sym,
                                   gate="free_cash", amount=round(amount, 2))
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event("gate_passed", "meanrev", sym,
                                       gate="circuit_breaker", amount=round(amount, 2))
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} blocked by circuit breaker: {halt}")
                trade_logger.log_event("gate_failed", "meanrev", sym,
                                       gate="circuit_breaker", reason=str(halt))
                continue
            result = broker.buy(
                sym, dollar_amount=amount,
                stop_loss_pct=config.MEANREV_STOP_PCT,
                take_profit_pct=None,  # no hard target (time-managed exit)
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event("order_skipped", "meanrev", sym,
                                       gate="broker_buy", reason=result.get("reason"))
                continue
            if not result.get("stop_attached"):
                log.error(f"  ✗ {sym} stop NOT attached — flattening")
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event("order_skipped", "meanrev", sym,
                                       gate="stop_attach", reason="stop-loss attach failed — flattened",
                                       qty=result["qty"], price=result["price"])
                continue

            log.info(f"  ✓ MeanRev {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.MEANREV_HOLD_DAYS}d)")
            trade_logger.log_event("order_placed", "meanrev", sym,
                                   qty=result["qty"], price=result["price"],
                                   stop=result["stop"], amount=round(amount, 2),
                                   hold_days=config.MEANREV_HOLD_DAYS)

            pead_track(sym, result["price"],
                       surprise_pct=c.get("rsi", 0),
                       report_date=datetime.date.today().isoformat(),
                       strategy="meanrev",
                       hold_days=config.MEANREV_HOLD_DAYS)
            send_trade_alert(
                action="BUY", ticker=sym, shares=result["qty"],
                price=result["price"], stop=result["stop"], target=None,
                reason=(f"MeanRev RSI={c['rsi']} BB={c['bb_position']:.0f}%"
                        f" momentum={c.get('momentum_pct', 0):+.1f}%"),
            )
            _mark_bought(sym, result)
            _append_trade_log({
                "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                "symbol": sym, "side": "buy", "qty": result.get("qty"),
                "price": result.get("price"), "stop": result.get("stop"),
                "target": None, "strategy": "meanrev",
                "rsi": c["rsi"], "bb_position": c["bb_position"],
                "exit_date": None, "exit_price": None, "pnl_pct": None,
            })
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — MeanRev stopping")
                break
        except Exception as e:
            log.error(f"  ✗ MeanRev {sym} failed: {e}")


def _run_insider(broker, cb, pv, slots, held, already_bought_today):
    """Insider P-Purchases: CEO/CFO conviction + cluster + $ value. Hold ~30d."""
    if screen_insider is None:
        log.warning("Insider: screener not loaded — see import error above — skipping")
        return
    log.info("Insider: screening P-Purchases via SEC EDGAR...")
    candidates = screen_insider()
    log.info(f"Insider: {len(candidates)} candidates")
    if not candidates:
        return

    for c in candidates:
        sym = c["symbol"]
        size_pct = config.INSIDER_SIZE_PCT
        amount = pv * size_pct

        trade_logger.log_event(
            "signal_detected", "insider", sym,
            insider_score=c["insider_score"], n_transactions=c["n_transactions"],
            total_dollar=c["total_dollar"],
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event("order_skipped", "insider", sym,
                                   gate="already_held", reason="already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event("order_skipped", "insider", sym,
                                   gate="idempotency", reason="already bought today")
            continue

        log.info(f"Insider BUY {sym} | score={c['insider_score']:.0f} "
                 f"txns={c['n_transactions']} total=${c['total_dollar']:,.0f} | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                trade_logger.log_event("gate_failed", "insider", sym,
                                       gate="free_cash", amount=round(amount, 2),
                                       reason="cannot free cash from SPY base")
                continue
            trade_logger.log_event("gate_passed", "insider", sym,
                                   gate="free_cash", amount=round(amount, 2))
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event("gate_passed", "insider", sym,
                                       gate="circuit_breaker", amount=round(amount, 2))
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                trade_logger.log_event("gate_failed", "insider", sym,
                                       gate="circuit_breaker", reason=str(halt))
                continue
            result = broker.buy(
                sym, dollar_amount=amount,
                stop_loss_pct=config.INSIDER_STOP_PCT,
                take_profit_pct=None,
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event("order_skipped", "insider", sym,
                                       gate="broker_buy", reason=result.get("reason"))
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event("order_skipped", "insider", sym,
                                       gate="stop_attach", reason="stop-loss attach failed — flattened",
                                       qty=result["qty"], price=result["price"])
                continue

            log.info(f"  ✓ Insider {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.INSIDER_HOLD_DAYS}d)")
            trade_logger.log_event("order_placed", "insider", sym,
                                   qty=result["qty"], price=result["price"],
                                   stop=result["stop"], amount=round(amount, 2),
                                   hold_days=config.INSIDER_HOLD_DAYS)

            pead_track(sym, result["price"],
                       surprise_pct=c.get("insider_score", 0),
                       report_date=datetime.date.today().isoformat(),
                       strategy="insider",
                       hold_days=config.INSIDER_HOLD_DAYS)
            send_trade_alert(
                action="BUY", ticker=sym, shares=result["qty"],
                price=result["price"], stop=result["stop"], target=None,
                reason=(f"Insider score={c['insider_score']:.0f}"
                        f" {c['n_transactions']} purchases"),
            )
            _mark_bought(sym, result)
            _append_trade_log({
                "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                "symbol": sym, "side": "buy", "qty": result.get("qty"),
                "price": result.get("price"), "stop": result.get("stop"),
                "target": None, "strategy": "insider",
                "insider_score": c["insider_score"],
                "total_dollar": c["total_dollar"],
                "exit_date": None, "exit_price": None, "pnl_pct": None,
            })
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — Insider stopping")
                break
        except Exception as e:
            log.error(f"  ✗ Insider {sym} failed: {e}")


def _run_squeeze(broker, cb, pv, slots, held, already_bought_today):
    """Short Squeeze: SI>15% + DTC>3 + bullish momentum. Hold ~21d."""
    if screen_squeeze is None:
        log.warning("Squeeze: screener not loaded — see import error above — skipping")
        return
    log.info("Squeeze: screening short interest...")
    candidates = screen_squeeze()
    log.info(f"Squeeze: {len(candidates)} candidates")
    if not candidates:
        return

    for c in candidates:
        sym = c["symbol"]
        size_pct = config.SQUEEZE_SIZE_PCT
        amount = pv * size_pct

        trade_logger.log_event(
            "signal_detected", "squeeze", sym,
            short_interest_pct=c["short_interest_pct"], days_to_cover=c["days_to_cover"],
            momentum_pct=c["momentum_pct"], score=c["score"],
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event("order_skipped", "squeeze", sym,
                                   gate="already_held", reason="already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event("order_skipped", "squeeze", sym,
                                   gate="idempotency", reason="already bought today")
            continue

        log.info(f"Squeeze BUY {sym} | SI={c['short_interest_pct']:.1f}%"
                 f" DTC={c['days_to_cover']:.1f}d mom={c['momentum_pct']:+.1f}% "
                 f"score={c['score']:.0f} | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                trade_logger.log_event("gate_failed", "squeeze", sym,
                                       gate="free_cash", amount=round(amount, 2),
                                       reason="cannot free cash from SPY base")
                continue
            trade_logger.log_event("gate_passed", "squeeze", sym,
                                   gate="free_cash", amount=round(amount, 2))
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event("gate_passed", "squeeze", sym,
                                       gate="circuit_breaker", amount=round(amount, 2))
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                trade_logger.log_event("gate_failed", "squeeze", sym,
                                       gate="circuit_breaker", reason=str(halt))
                continue
            result = broker.buy(
                sym, dollar_amount=amount,
                stop_loss_pct=config.SQUEEZE_STOP_PCT,
                take_profit_pct=None,
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event("order_skipped", "squeeze", sym,
                                       gate="broker_buy", reason=result.get("reason"))
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event("order_skipped", "squeeze", sym,
                                       gate="stop_attach", reason="stop-loss attach failed — flattened",
                                       qty=result["qty"], price=result["price"])
                continue

            log.info(f"  ✓ Squeeze {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.SQUEEZE_HOLD_DAYS}d)")
            trade_logger.log_event("order_placed", "squeeze", sym,
                                   qty=result["qty"], price=result["price"],
                                   stop=result["stop"], amount=round(amount, 2),
                                   hold_days=config.SQUEEZE_HOLD_DAYS)

            pead_track(sym, result["price"],
                       surprise_pct=c.get("score", 0),
                       report_date=datetime.date.today().isoformat(),
                       strategy="squeeze",
                       hold_days=config.SQUEEZE_HOLD_DAYS)
            send_trade_alert(
                action="BUY", ticker=sym, shares=result["qty"],
                price=result["price"], stop=result["stop"], target=None,
                reason=(f"Squeeze SI={c['short_interest_pct']:.1f}%"
                        f" DTC={c['days_to_cover']:.1f}d mom={c['momentum_pct']:+.1f}%"),
            )
            _mark_bought(sym, result)
            _append_trade_log({
                "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                "symbol": sym, "side": "buy", "qty": result.get("qty"),
                "price": result.get("price"), "stop": result.get("stop"),
                "target": None, "strategy": "squeeze",
                "si_pct": c["short_interest_pct"],
                "days_to_cover": c["days_to_cover"],
                "momentum_pct": c["momentum_pct"],
                "exit_date": None, "exit_price": None, "pnl_pct": None,
            })
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — Squeeze stopping")
                break
        except Exception as e:
            log.error(f"  ✗ Squeeze {sym} failed: {e}")


def _run_breakout(broker, cb, pv, slots, held, already_bought_today):
    """Breakout: price above 50d resistance + 1.5x volume confirmation. Hold ~21d."""
    if screen_breakout is None:
        log.warning("Breakout: screener not loaded — see import error above — skipping")
        return
    log.info("Breakout: screening for 50d resistance clears...")
    candidates = screen_breakout()
    log.info(f"Breakout: {len(candidates)} candidates")
    if not candidates:
        return

    for c in candidates:
        sym = c["symbol"]
        size_pct = config.BREAKOUT_SIZE_PCT
        amount = pv * size_pct

        trade_logger.log_event(
            "signal_detected", "breakout", sym,
            price=c["price"], clearance_pct=c["clearance_pct"],
            volume_ratio=c["volume_ratio"], atr_pct=c["atr_pct"], score=c["score"],
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event("order_skipped", "breakout", sym,
                                   gate="already_held", reason="already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event("order_skipped", "breakout", sym,
                                   gate="idempotency", reason="already bought today")
            continue

        log.info(f"Breakout BUY {sym} | price=${c['price']:.2f} "
                 f"clearance={c['clearance_pct']:+.2f}% vol={c['volume_ratio']}x "
                 f"ATR={c['atr_pct']:.1f}% score={c['score']:.0f} | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                trade_logger.log_event("gate_failed", "breakout", sym,
                                       gate="free_cash", amount=round(amount, 2),
                                       reason="cannot free cash from SPY base")
                continue
            trade_logger.log_event("gate_passed", "breakout", sym,
                                   gate="free_cash", amount=round(amount, 2))
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event("gate_passed", "breakout", sym,
                                       gate="circuit_breaker", amount=round(amount, 2))
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                trade_logger.log_event("gate_failed", "breakout", sym,
                                       gate="circuit_breaker", reason=str(halt))
                continue
            result = broker.buy(
                sym, dollar_amount=amount,
                stop_loss_pct=config.BREAKOUT_STOP_PCT,
                take_profit_pct=None,
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event("order_skipped", "breakout", sym,
                                       gate="broker_buy", reason=result.get("reason"))
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event("order_skipped", "breakout", sym,
                                       gate="stop_attach", reason="stop-loss attach failed — flattened",
                                       qty=result["qty"], price=result["price"])
                continue

            log.info(f"  ✓ Breakout {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.BREAKOUT_HOLD_DAYS}d)")
            trade_logger.log_event("order_placed", "breakout", sym,
                                   qty=result["qty"], price=result["price"],
                                   stop=result["stop"], amount=round(amount, 2),
                                   hold_days=config.BREAKOUT_HOLD_DAYS)

            pead_track(sym, result["price"],
                       surprise_pct=c.get("score", 0),
                       report_date=datetime.date.today().isoformat(),
                       strategy="breakout",
                       hold_days=config.BREAKOUT_HOLD_DAYS)
            send_trade_alert(
                action="BUY", ticker=sym, shares=result["qty"],
                price=result["price"], stop=result["stop"], target=None,
                reason=(f"Breakout clearance={c['clearance_pct']:+.2f}%"
                        f" vol={c['volume_ratio']}x ATR={c['atr_pct']:.1f}%"),
            )
            _mark_bought(sym, result)
            _append_trade_log({
                "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                "symbol": sym, "side": "buy", "qty": result.get("qty"),
                "price": result.get("price"), "stop": result.get("stop"),
                "target": None, "strategy": "breakout",
                "clearance_pct": c["clearance_pct"],
                "volume_ratio": c["volume_ratio"],
                "atr_pct": c["atr_pct"],
                "exit_date": None, "exit_price": None, "pnl_pct": None,
            })
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — Breakout stopping")
                break
        except Exception as e:
            log.error(f"  ✗ Breakout {sym} failed: {e}")


def _run_earnmom(broker, cb, pv, slots, held, already_bought_today):
    """Earnings Momentum: beat 8-45d ago, still drifting up. Hold ~35d."""
    if screen_earnmom is None:
        log.warning("EarnMom: screener not loaded — see import error above — skipping")
        return
    log.info("EarnMom: screening earnings beats that still have momentum drift...")
    candidates = screen_earnmom()
    log.info(f"EarnMom: {len(candidates)} candidates")
    if not candidates:
        return

    for c in candidates:
        sym = c["symbol"]
        size_pct = config.EARNMOM_SIZE_PCT
        amount = pv * size_pct

        trade_logger.log_event(
            "signal_detected", "earnmom", sym,
            surprise_pct=c["surprise_pct"], age_days=c["age_days"],
            drift_pct=c["drift_pct"], score=c["score"],
            report_date=c.get("report_date"),
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event("order_skipped", "earnmom", sym,
                                   gate="already_held", reason="already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event("order_skipped", "earnmom", sym,
                                   gate="idempotency", reason="already bought today")
            continue

        log.info(f"EarnMom BUY {sym} | surprise={c['surprise_pct']:+.1f}% "
                 f"age={c['age_days']}d drift={c['drift_pct']:+.1f}% score={c['score']:.0f} | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                trade_logger.log_event("gate_failed", "earnmom", sym,
                                       gate="free_cash", amount=round(amount, 2),
                                       reason="cannot free cash from SPY base")
                continue
            trade_logger.log_event("gate_passed", "earnmom", sym,
                                   gate="free_cash", amount=round(amount, 2))
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event("gate_passed", "earnmom", sym,
                                       gate="circuit_breaker", amount=round(amount, 2))
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                trade_logger.log_event("gate_failed", "earnmom", sym,
                                       gate="circuit_breaker", reason=str(halt))
                continue
            result = broker.buy(
                sym, dollar_amount=amount,
                stop_loss_pct=config.EARNMOM_STOP_PCT,
                take_profit_pct=None,
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event("order_skipped", "earnmom", sym,
                                       gate="broker_buy", reason=result.get("reason"))
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event("order_skipped", "earnmom", sym,
                                       gate="stop_attach", reason="stop-loss attach failed — flattened",
                                       qty=result["qty"], price=result["price"])
                continue

            log.info(f"  ✓ EarnMom {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.EARNMOM_HOLD_DAYS}d)")
            trade_logger.log_event("order_placed", "earnmom", sym,
                                   qty=result["qty"], price=result["price"],
                                   stop=result["stop"], surprise_pct=c["surprise_pct"],
                                   amount=round(amount, 2), hold_days=config.EARNMOM_HOLD_DAYS)

            pead_track(sym, result["price"],
                       surprise_pct=c.get("surprise_pct", 0),
                       report_date=c.get("report_date", datetime.date.today().isoformat()),
                       strategy="earnmom",
                       hold_days=config.EARNMOM_HOLD_DAYS)
            send_trade_alert(
                action="BUY", ticker=sym, shares=result["qty"],
                price=result["price"], stop=result["stop"], target=None,
                reason=(f"EarnMom surprise={c['surprise_pct']:+.1f}%"
                        f" drift={c['drift_pct']:+.1f}% age={c['age_days']}d"),
            )
            _mark_bought(sym, result)
            _append_trade_log({
                "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                "symbol": sym, "side": "buy", "qty": result.get("qty"),
                "price": result.get("price"), "stop": result.get("stop"),
                "target": None, "strategy": "earnmom",
                "surprise_pct": c["surprise_pct"],
                "drift_pct": c["drift_pct"],
                "age_days": c["age_days"],
                "exit_date": None, "exit_price": None, "pnl_pct": None,
            })
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — EarnMom stopping")
                break
        except Exception as e:
            log.error(f"  ✗ EarnMom {sym} failed: {e}")


def run():
    config.validate()
    now = datetime.datetime.now(ET)
    logger.banner(log, f"MARKET OPEN ROUTINE — fired {now.strftime('%A %Y-%m-%d %H:%M %Z')}")

    broker = BrokerClient()
    pv = broker.portfolio_value()
    day_start = load_day_start_value(pv)
    cb = _build_breaker(broker, day_start)

    reconciled = _reconcile_closed_trades(broker)
    if reconciled:
        log.info(f"Reconciled {reconciled} closed trades from Alpaca order history into trade_log.jsonl")

    if not broker.is_market_open():
        log.error("Market is CLOSED — aborting without polling")
        return
    log.info("Market is OPEN ✓")

    allowed, why = is_entry_window()
    if not allowed:
        log.warning(f"Entry blocked: {why}")
        return
    log.info(f"Entry timing: {why}")

    pos_count = broker.position_count()
    slots = [min(MAX_BUYS, config.MAX_OPEN_POSITIONS - pos_count)]  # mutable: handlers decrement in-place

    log.info(f"Portfolio: ${pv:,.2f} | Positions: {pos_count} | Slots: {slots[0]}")

    if circuit_breaker_tripped(pv, day_start):
        day_pnl = (pv - day_start) / day_start * 100
        log.warning(f"CIRCUIT BREAKER: day P&L {day_pnl:+.2f}% — NO new entries")
        return

    if slots[0] <= 0:
        log.info("No slots — done")
        return

    held = set()
    try:
        held = {p.symbol for p in broker.get_positions()}
        log.info(f"Currently holding: {sorted(held) or 'none'}")
    except Exception as e:
        log.warning(f"Could not fetch holdings (non-blocking): {e}")

    already_bought_today = _load_today_bought()
    if already_bought_today:
        log.info(f"Already bought today (idempotency): {sorted(already_bought_today)}")

    # ── Regime gate (shared by both strategies) ─────────────────────────────
    try:
        from core.screener import fetch_bars
        spy_bars = (fetch_bars(["SPY"], days=400) or {}).get("SPY") or []
    except Exception as e:
        log.warning(f"Regime gate SPY bars fetch failed (non-blocking): {e}")
        spy_bars = []
    if spy_bars:
        highs  = [b["high"]   for b in spy_bars]
        lows   = [b["low"]    for b in spy_bars]
        closes = [b["close"]  for b in spy_bars]
        reg = classify(highs, lows, closes)
        log.info(f"Regime gate: state={reg.state} trend={reg.trend} adx={reg.adx:.1f} sma50={reg.sma50:.2f} sma200={reg.sma200:.2f} reason={reg.reason}")
        if not reg.can_trade:
            log.warning(f"REGIME GATE: STAND_DOWN — {reg.reason} — holding cash, no screening")
            return
    else:
        log.warning("Regime gate skipped: no SPY bars available — proceeding without gate")

    # ── STRATEGY ROUTER ───────────────────────────────────────────────────────
    # Run each strategy in priority order (PEAD → MEANREV → INSIDER → SQUEEZE →
    # BREAKOUT → EARNMOM). Each runner consumes from the shared `slots` pool.
    # Held-set and already_bought_today accumulate across runners so the same
    # symbol is never double-bought within a single run.
    STRATEGY_HANDLERS = {
        "pead":    _run_pead,
        "meanrev": _run_meanrev,
        "insider": _run_insider,
        "squeeze": _run_squeeze,
        "breakout": _run_breakout,
        "earnmom": _run_earnmom,
    }
    log.info(f"Strategy modes: {[s.upper() for s in config.STRATEGY_MODES]}")

    for strategy in config.STRATEGY_MODES:
        if slots[0] <= 0:
            log.info(f"No slots remaining — stopping strategy loop")
            break

        handler = STRATEGY_HANDLERS.get(strategy)
        if handler is None:
            log.warning(f"Unknown strategy '{strategy}' — skipping")
            continue

        log.info(f"=== {strategy.upper()} RUNNER ===")
        try:
            handler(broker, cb, pv, slots, held, already_bought_today)
        except Exception as e:
            log.error(f"Strategy {strategy.upper()} runner raised: {e}")

    log.info("All strategy runners complete")

    # Rebalance idle cash back into SPY
    spy_log(broker)
    spy_result = rebalance_to_spy(broker)
    if spy_result["action"] not in ("none", "disabled"):
        log.info(f"SPY base: {spy_result['action']} {spy_result.get('qty', 0)} shares")

    logger.banner(log, "MARKET OPEN COMPLETE")


if __name__ == "__main__":
    run()
