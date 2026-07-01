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
from kelly_sizing import KellySizer, stats_from_trades
from core.earnings_screener import screen_earnings
from core.pead_tracker import add_position as pead_track
from core.spy_base import rebalance_to_spy, free_cash_for_pead, log_status as spy_log

# Strategy screeners — fail gracefully if FMP unavailable
try:
    from core.meanrev_screener import screen as screen_meanrev
except Exception:
    screen_meanrev = None
try:
    from core.insider_screener import screen as screen_insider
except Exception:
    screen_insider = None
try:
    from core.squeeze_screener import screen as screen_squeeze
except Exception:
    screen_squeeze = None
try:
    from core.breakout_screener import screen as screen_breakout
except Exception:
    screen_breakout = None
try:
    from core.earnings_momentum_screener import screen as screen_earnmom
except Exception:
    screen_earnmom = None

log = logger.setup("market_open")
ET  = pytz.timezone("America/New_York")


def _build_breaker(broker: BrokerClient) -> CircuitBreaker:
    return CircuitBreaker(
        get_account=broker.get_account,
        get_positions=broker.get_positions,
        max_open_positions=config.MAX_OPEN_POSITIONS,
        max_position_pct=config.MAX_POSITION_SIZE_PCT,
        max_daily_loss=0.03,
    )

DAY_START_PATH = os.path.join(config.STATE_DIR, "day_start_value.json")
TODAY_BOUGHT_PATH = os.path.join(config.STATE_DIR, "today_bought.json")
TRADE_LOG_PATH = os.path.join(config.STATE_DIR, "trade_log.jsonl")
MAX_BUYS       = 3
KELLY_WINDOW_TRADES = 100


def _load_trade_returns(n: int = KELLY_WINDOW_TRADES) -> list[float]:
    """Last n closed-trade return decimals from state/trade_log.jsonl.

    Entries with a null pnl (still open, or close-event not yet recorded) are
    skipped — only realized trades contribute to Kelly stats.
    """
    if not os.path.exists(TRADE_LOG_PATH):
        return []
    out: list[float] = []
    try:
        with open(TRADE_LOG_PATH) as f:
            lines = f.readlines()
        for line in lines[-n:]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            pnl = rec.get("pnl_pct")
            if pnl is None:
                continue
            out.append(float(pnl) / 100.0)
    except OSError as e:
        log.warning(f"trade_log read failed (non-blocking): {e}")
    return out


def _append_trade_log(entry: dict) -> None:
    """Append one JSONL record to state/trade_log.jsonl. Non-blocking on error."""
    try:
        with open(TRADE_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log.warning(f"trade_log append failed: {e}")


def _compute_kelly_shadow(equity: float) -> tuple[float, str]:
    """Quarter-Kelly shadow. Returns (fraction, reason). fraction is what Kelly
    WOULD size — the live code does NOT use it. Logs only."""
    sizer = KellySizer(kelly_fraction=0.25, max_position_pct=0.05)
    rets = _load_trade_returns()
    n = len(rets)
    wr, aw, al, _ = stats_from_trades(rets)
    res = sizer.size(equity, win_rate=wr, avg_win=aw, avg_loss=al, n_trades=n)
    return res.fraction, res.reason


def _reconcile_closed_trades(broker) -> int:
    """For each buy row in trade_log.jsonl with pnl_pct=null, look up the matching
    closed SELL order on Alpaca. If found, fill exit_price/exit_date/pnl_pct in
    place and rewrite the JSONL. Returns the number of rows reconciled today.

    Called at the top of run() so the rolling-100 stats used by the Kelly shadow
    reflect realized exits from yesterday and earlier.
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

    buys_taken = 0
    for c in candidates[:slots]:
        sym = c["symbol"]
        surprise = c["surprise_pct"]
        price = c.get("price", 0)

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            continue

        size_pct = config.PEAD_SIZE_PCT
        amount = pv * size_pct

        log.info(f"PEAD BUY {sym} | surprise={surprise:+.1f}% | "
                 f"size={size_pct*100:.0f}% | ${amount:,.0f}")
        try:
            # Free SPY cash if needed for this PEAD entry
            if not free_cash_for_pead(broker, amount):
                log.warning(f"✗ {sym} SKIP — cannot free ${amount:,.0f} from SPY base")
                continue

            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
            except TradingHalted as halt:
                log.warning(f"✗ {sym} blocked by circuit breaker: {halt}")
                continue

            # PEAD uses wide stop (-15%), NO take-profit (time exit at 60d)
            # Set take_profit very wide (99%) so OCO doesn't trigger early
            result = broker.buy(
                sym,
                dollar_amount=amount,
                stop_loss_pct=config.PEAD_STOP_PCT,
                take_profit_pct=0.99,
            )
            if result.get("blocked"):
                log.warning(f"✗ {sym} buy blocked: {result.get('reason')}")
                continue
            if not result.get("stop_attached"):
                log.error(f"✗ {sym} bought but stop NOT attached — flattening")
                broker.sell(sym, qty=result["qty"])
                send_trade_alert(
                    action="FLATTEN", ticker=sym, shares=result["qty"],
                    price=result["price"],
                    reason="PEAD stop-loss attach failed — position rejected",
                )
                continue

            log.info(f"✓ PEAD {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.PEAD_HOLD_DAYS}d)")

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
        log.warning("MeanRev: screener unavailable (FMP?) — skipping")
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

        if sym in held:
            continue
        if sym in already_bought_today:
            continue

        log.info(f"MeanRev BUY {sym} | RSI={c['rsi']} BBpos={c['bb_position']:.0f}% "
                 f"momentum={c.get('momentum_pct', 0):+.1f}% | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash from SPY base")
                continue
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} blocked by circuit breaker: {halt}")
                continue
            result = broker.buy(
                sym, dollar_amount=amount,
                stop_loss_pct=config.MEANREV_STOP_PCT,
                take_profit_pct=0.99,   # time-managed, no hard target
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                continue
            if not result.get("stop_attached"):
                log.error(f"  ✗ {sym} stop NOT attached — flattening")
                broker.sell(sym, qty=result["qty"])
                continue

            log.info(f"  ✓ MeanRev {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.MEANREV_HOLD_DAYS}d)")

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
        except Exception as e:
            log.error(f"  ✗ MeanRev {sym} failed: {e}")


def _run_insider(broker, cb, pv, slots, held, already_bought_today):
    """Insider P-Purchases: CEO/CFO conviction + cluster + $ value. Hold ~30d."""
    if screen_insider is None:
        log.warning("Insider: screener unavailable (FMP?) — skipping")
        return
    log.info("Insider: screening P-Purchases via FMP insider-trading...")
    candidates = screen_insider()
    log.info(f"Insider: {len(candidates)} candidates")
    if not candidates:
        return

    for c in candidates:
        sym = c["symbol"]
        size_pct = config.INSIDER_SIZE_PCT
        amount = pv * size_pct

        if sym in held:
            continue
        if sym in already_bought_today:
            continue

        log.info(f"Insider BUY {sym} | score={c['insider_score']:.0f} "
                 f"txns={c['n_transactions']} total=${c['total_dollar']:,.0f} | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                continue
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                continue
            result = broker.buy(
                sym, dollar_amount=amount,
                stop_loss_pct=config.INSIDER_STOP_PCT,
                take_profit_pct=0.99,
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                continue

            log.info(f"  ✓ Insider {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.INSIDER_HOLD_DAYS}d)")

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
        except Exception as e:
            log.error(f"  ✗ Insider {sym} failed: {e}")


def _run_squeeze(broker, cb, pv, slots, held, already_bought_today):
    """Short Squeeze: SI>15% + DTC>3 + bullish momentum. Hold ~21d."""
    if screen_squeeze is None:
        log.warning("Squeeze: screener unavailable (FMP?) — skipping")
        return
    log.info("Squeeze: screening short interest via FMP...")
    candidates = screen_squeeze()
    log.info(f"Squeeze: {len(candidates)} candidates")
    if not candidates:
        return

    for c in candidates:
        sym = c["symbol"]
        size_pct = config.SQUEEZE_SIZE_PCT
        amount = pv * size_pct

        if sym in held:
            continue
        if sym in already_bought_today:
            continue

        log.info(f"Squeeze BUY {sym} | SI={c['short_interest_pct']:.1f}%"
                 f" DTC={c['days_to_cover']:.1f}d mom={c['momentum_pct']:+.1f}% "
                 f"score={c['score']:.0f} | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                continue
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                continue
            result = broker.buy(
                sym, dollar_amount=amount,
                stop_loss_pct=config.SQUEEZE_STOP_PCT,
                take_profit_pct=0.99,
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                continue

            log.info(f"  ✓ Squeeze {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.SQUEEZE_HOLD_DAYS}d)")

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
        except Exception as e:
            log.error(f"  ✗ Squeeze {sym} failed: {e}")


def _run_breakout(broker, cb, pv, slots, held, already_bought_today):
    """Breakout: price above 50d resistance + 1.5x volume confirmation. Hold ~21d."""
    if screen_breakout is None:
        log.warning("Breakout: screener unavailable (FMP?) — skipping")
        return
    log.info("Breakout: screening for 50d resistance clears via FMP...")
    candidates = screen_breakout()
    log.info(f"Breakout: {len(candidates)} candidates")
    if not candidates:
        return

    for c in candidates:
        sym = c["symbol"]
        size_pct = config.BREAKOUT_SIZE_PCT
        amount = pv * size_pct

        if sym in held:
            continue
        if sym in already_bought_today:
            continue

        log.info(f"Breakout BUY {sym} | price=${c['price']:.2f} "
                 f"clearance={c['clearance_pct']:+.2f}% vol={c['volume_ratio']}x "
                 f"ATR={c['atr_pct']:.1f}% score={c['score']:.0f} | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                continue
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                continue
            result = broker.buy(
                sym, dollar_amount=amount,
                stop_loss_pct=config.BREAKOUT_STOP_PCT,
                take_profit_pct=0.99,
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                continue

            log.info(f"  ✓ Breakout {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.BREAKOUT_HOLD_DAYS}d)")

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
        except Exception as e:
            log.error(f"  ✗ Breakout {sym} failed: {e}")


def _run_earnmom(broker, cb, pv, slots, held, already_bought_today):
    """Earnings Momentum: beat 8-45d ago, still drifting up. Hold ~35d."""
    if screen_earnmom is None:
        log.warning("EarnMom: screener unavailable (FMP?) — skipping")
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

        if sym in held:
            continue
        if sym in already_bought_today:
            continue

        log.info(f"EarnMom BUY {sym} | surprise={c['surprise_pct']:+.1f}% "
                 f"age={c['age_days']}d drift={c['drift_pct']:+.1f}% score={c['score']:.0f} | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                continue
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                continue
            result = broker.buy(
                sym, dollar_amount=amount,
                stop_loss_pct=config.EARNMOM_STOP_PCT,
                take_profit_pct=0.99,
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                continue

            log.info(f"  ✓ EarnMom {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.EARNMOM_HOLD_DAYS}d)")

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
        except Exception as e:
            log.error(f"  ✗ EarnMom {sym} failed: {e}")


def run():
    config.validate()
    now = datetime.datetime.now(ET)
    logger.banner(log, f"MARKET OPEN ROUTINE — fired {now.strftime('%A %Y-%m-%d %H:%M %Z')}")

    broker = BrokerClient()
    cb = _build_breaker(broker)

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

    pv        = broker.portfolio_value()
    pos_count = broker.position_count()
    slots     = min(MAX_BUYS, config.MAX_OPEN_POSITIONS - pos_count)

    log.info(f"Portfolio: ${pv:,.2f} | Positions: {pos_count} | Slots: {slots}")

    day_start = load_day_start_value(pv)
    if circuit_breaker_tripped(pv, day_start):
        day_pnl = (pv - day_start) / day_start * 100
        log.warning(f"CIRCUIT BREAKER: day P&L {day_pnl:+.2f}% — NO new entries")
        return

    if slots <= 0:
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
        from core.screener import _fetch_bars
        spy_bars = (_fetch_bars(["SPY"], days=400) or {}).get("SPY") or []
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
        if slots <= 0:
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
    return
    log.info(f"Universe: {len(universe)} symbols → screening...")
    candidates = screen(universe)
    log.info(f"Got {len(candidates)} candidates")

    if not candidates:
        log.info("No candidates — done")
        return

    passed = []
    for c in candidates:
        sym   = c["symbol"]
        price = c.get("price", 0)
        rs    = c.get("rs_vs_spy")
        gap   = c.get("gap_pct", 0)
        relv  = c.get("rel_volume", 0)
        score = c.get("score", 0)

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            continue

        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today (idempotency)")
            continue

        if not (config.MIN_PRICE <= price <= config.MAX_PRICE):
            log.info(f"  ✗ {sym} SKIP — price ${price:.2f} out of band")
            continue

        if rs is not None and rs < config.MIN_RS_RATING:
            log.info(f"  ✗ {sym} REJECT — RS {rs:+.1f}% < SPY+{config.MIN_RS_RATING}")
            continue

        if abs(gap) > config.MAX_GAP_PCT:
            log.info(f"  ✗ {sym} REJECT — gap {gap:+.1f}% > {config.MAX_GAP_PCT}%")
            continue

        if score < config.MIN_COMPOSITE_SCORE:
            log.info(f"  ✗ {sym} REJECT — score {score} below {config.MIN_COMPOSITE_SCORE} floor")
            continue

        rs_str  = f"RS{rs:+.1f}%" if rs is not None else "RS n/a"
        log.info(f"  ✓ {sym} PASS — ${price:.2f} score={score} {rs_str} gap={gap:+.1f}% relvol={relv}")
        passed.append(c)

    log.info(f"Passed all skills: {len(passed)}/{len(candidates)}")

    if not passed:
        log.info("Nothing passed skills — done")
        return

    # ── COMPOSITE SCORING ───────────────────────────────────────────────────
    # Every GROUP A skill (Alpaca/no-API) contributes a 0-100 sub-score; GROUP B
    # (FMP) sub-scores fail gracefully to neutral 50 on any error/429. The
    # survivors are ranked by the regime-scaled composite, then the top `slots`
    # are bought. All existing filters above are unchanged; buy()/OCO untouched.
    log.info("Composite scoring (GROUP A skills + graceful GROUP B)...")
    passed_syms = [c["symbol"] for c in passed]
    ctx = composite.build_context(extra_symbols=passed_syms)
    log.info(f"Market regime score={ctx['regime_score']} → composite mult={ctx['regime_mult']} "
             f"| sector momentum: {ctx['sector_mom']}")

    bars_map = {}
    try:
        from core.screener import _fetch_bars
        bars_map = _fetch_bars(passed_syms, days=60)
    except Exception as e:
        log.warning(f"Composite bar fetch failed (non-blocking, bar-scores→neutral): {e}")

    scored = []
    for c in passed:
        result = composite.compute_composite(c, bars_map.get(c["symbol"]), ctx)
        c["composite"] = result["composite"]
        c["composite_final"] = result["final"]
        c["composite_breakdown"] = result["breakdown"]
        log.info(composite.format_breakdown(result))
        scored.append(c)

    scored.sort(key=lambda x: x.get("composite_final", 0), reverse=True)
    ranked = " > ".join(f"{c['symbol']}({c.get('composite_final', 0):.1f})" for c in scored)
    log.info(f"Composite ranking: {ranked}")
    passed = scored

    buys_taken = 0
    for c in passed[:slots]:
        sym    = c["symbol"]
        score  = c.get("score", 0)
        rs     = c.get("rs_vs_spy")
        comp     = c.get("composite_final", 0)
        size_pct = config.MAX_POSITION_SIZE_PCT
        amount   = pv * size_pct

        kelly_frac, kelly_reason = _compute_kelly_shadow(pv)
        log.info(f"KELLY_SHADOW {sym} | would_size={kelly_frac:.2%} | flat_size={size_pct:.2%} | diff={kelly_frac - size_pct:+.2%} | {kelly_reason}")

        log.info(f"BUYING {sym} | composite={comp:.1f} (vcp={score}) | size={size_pct*100:.0f}% | ${amount:,.0f}")
        try:
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
            except TradingHalted as halt:
                log.warning(f"✗ {sym} blocked by circuit breaker: {halt.reason} — {halt}")
                continue
            result = broker.buy(sym, dollar_amount=amount)
            if result.get("blocked"):
                log.warning(f"✗ {sym} buy blocked by guardrail: {result.get('reason')}")
                continue
            if not result.get("stop_attached"):
                log.error(f"✗ {sym} bought but stop NOT attached — flattening position")
                broker.sell(sym, qty=result["qty"])
                send_trade_alert(
                    action="FLATTEN",
                    ticker=sym,
                    shares=result["qty"],
                    price=result["price"],
                    reason="stop-loss attach failed at entry — unprotected position rejected",
                )
                continue
            log.info(f"✓ {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} TP={result['target']}")
            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=result["target"],
                reason=f"composite={comp:.1f} VCP={score}" + (f" RS{rs:+.0f}%" if rs is not None else ""),
            )
            _mark_bought(sym, result)
            _append_trade_log({
                "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                "symbol": sym,
                "side": "buy",
                "qty": result.get("qty"),
                "price": result.get("price"),
                "stop": result.get("stop"),
                "target": result.get("target"),
                "exit_date": None,
                "exit_price": None,
                "pnl_pct": None,
            })
            buys_taken += 1
        except Exception as e:
            log.error(f"✗ {sym} buy failed: {e}")

    log.info(f"Market open complete | Buys taken: {buys_taken}")
    logger.banner(log, "MARKET OPEN COMPLETE")


if __name__ == "__main__":
    run()
