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
from core import composite
from core.universe import build_universe
from circuit_breaker import CircuitBreaker, TradingHalted, EmergencyLiquidation
from regime_gate import classify
from core.earnings_screener import screen_earnings
from core.pead_tracker import add_position as pead_track
from core.spy_base import rebalance_to_spy, free_cash_for_pead, log_status as spy_log, is_base_symbol
from core import trade_logger

log = logger.setup("market_open")

import requests as _req  # noqa: F401
from functools import lru_cache

# ── SECTOR CONCENTRATION GUARD helpers ──────────────────────────────────────
# MAX_PER_SECTOR enforced across all strategies within a single run
_SECTOR_CACHE: dict = {}

@lru_cache(maxsize=500)
def _fetch_symbol_sector(symbol: str, api_key: str) -> str | None:
    """Look up GICS sector via FMP profile. Cached in-process."""
    if not api_key:
        return None
    try:
        url = f"https://financialmodelingprep.com/api/v3/profile/{symbol}?apikey={api_key}"
        resp = _req.get(url, timeout=10)
        if resp.ok:
            data = resp.json()
            if data and isinstance(data, list) and len(data):
                sector = (data[0].get("sector") or "").strip()
                return sector or None
    except Exception:
        pass
    return None


def _build_sector_counts(broker, fmp_key: str) -> dict:
    """Count open positions per GICS sector for the current held portfolio."""
    from collections import Counter
    counts: Counter = Counter()
    for p in broker.get_positions():
        if is_base_symbol(p.symbol):
            continue
        sector = (getattr(p, "sector", None) or
                  _fetch_symbol_sector(p.symbol, fmp_key))
        if sector:
            counts[sector] += 1
    return dict(counts)


def _sector_gate(symbol: str, sector_counts: dict, fmp_key: str,
                 strategy: str, log) -> bool:
    """
    Gate: returns True (allowed) if sector not at MAX_PER_SECTOR capacity.
    Marks sector consumed on pass; logs + returns False on block.
    """
    max_per = getattr(config, "MAX_PER_SECTOR", 2)
    sector = _fetch_symbol_sector(symbol, fmp_key)
    if sector is None:
        return True  # no FMP — circuit breaker gates on notional instead
    current = sector_counts.get(sector, 0)
    if current >= max_per:
        log.info(f"  SKIP {symbol} — sector {sector!r} at {current}/{max_per}")
        trade_logger.log_event(
            "gate_failed", strategy, symbol, gate="sector_concentration",
            reason=f"sector {sector} at {current}/{max_per}",
            sector=sector, current=current, cap=max_per,
        )
        return False
    sector_counts[sector] = current + 1
    return True

ET  = pytz.timezone("America/New_York")

# Populated from state/market_brief_<date>.json at run() start.
# Read by strategy runners without changing their signatures.
_today_brief: dict = {}

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
try:
    from core.gapfill_screener import screen as screen_gapfill
except Exception as e:
    log.error("GapFill screener import failed: %s", e)
    screen_gapfill = None
try:
    from core.momentum_screener import screen as screen_momentum
except Exception as e:
    log.error("Momentum screener import failed: %s", e)
    screen_momentum = None
try:
    from core.sector_screener import screen as screen_sector
except Exception as e:
    log.error("Sector screener import failed: %s", e)
    screen_sector = None


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


def _run_pead(broker, cb, pv, slots, held, already_bought_today, sector_counts):
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


        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "pead", log):
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
            except EmergencyLiquidation as emerg:
                log.error(f"✗ EMERGENCY LIQUIDATION — circuit breaker: {emerg}")
                trade_logger.log_event("gate_failed", "pead", sym,
                                       gate="emergency_liquidation", reason=str(emerg))
                # Propagate so market_open can close all positions before returning
                raise
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

def _run_meanrev(broker, cb, pv, slots, held, already_bought_today, sector_counts):
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

        # News filter — skip if pre_market research flagged bad sentiment
        _news = _today_brief.get("stock_news", {}).get(sym, {})
        if _news.get("skip"):
            log.info(f"  ✗ {sym} SKIP — news risk: {_news.get('reason', 'flagged by research')}")
            trade_logger.log_event("order_skipped", "meanrev", sym,
                                   gate="news_filter", reason=_news.get("reason", ""))
            continue

        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "meanrev", log):
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


def _run_insider(broker, cb, pv, slots, held, already_bought_today, sector_counts):
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


        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "insider", log):
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
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event("gate_failed", "insider", sym,
                                       gate="emergency_liquidation", reason=str(emerg))
                raise
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


def _run_squeeze(broker, cb, pv, slots, held, already_bought_today, sector_counts):
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


        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "squeeze", log):
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
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event("gate_failed", "squeeze", sym,
                                       gate="emergency_liquidation", reason=str(emerg))
                raise
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


def _run_breakout(broker, cb, pv, slots, held, already_bought_today, sector_counts):
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


        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "breakout", log):
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
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event("gate_failed", "breakout", sym,
                                       gate="emergency_liquidation", reason=str(emerg))
                raise
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


def _run_earnmom(broker, cb, pv, slots, held, already_bought_today, sector_counts):
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


        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "earnmom", log):
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
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event("gate_failed", "earnmom", sym,
                                       gate="emergency_liquidation", reason=str(emerg))
                raise
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


# ── Gap Fill runner ──────────────────────────────────────────────────────────
def _run_gapfill(broker, cb, pv, slots, held, already_bought_today, sector_counts):
    """Gap Fill: fade morning gaps. Gap-up = short spike, gap-down = bounce.
    Hold: max 4 hours or until target/stop hit."""
    if screen_gapfill is None:
        log.warning("GapFill: screener not loaded — skip")
        return
    log.info("GapFill: screening morning gaps...")
    candidates = screen_gapfill()
    log.info(f"GapFill: {len(candidates)} candidates")
    if not candidates:
        return
    for c in candidates:
        sym = c["symbol"]
        if slots[0] <= 0:
            log.info("Slots exhausted — GapFill stopping")
            break
        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            continue

        # Sector guard (gap fill trades are short-hold, treat as satellite)
        fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, fkp, "gapfill", log):
            continue

        amount = pv * 0.03  # gap fills are short-hold, size accordingly
        log.info(f"GapFill BUY {sym} | gap={c['gap_pct']:+.2f}% "
                 f"price=${c['price']:.2f} prior_close=${c['prior_close']:.2f}")
        trade_logger.log_event("signal_detected", "gapfill", sym,
                               price=c["price"], gap_pct=c["gap_pct"],
                               prior_close=c["prior_close"], target=c["target"])
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                continue
            cb.check_before_order(intended_notional=amount, symbol=sym)
        except EmergencyLiquidation as emerg:
            log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
            raise
        except TradingHalted as halt:
            log.warning(f"  ✗ {sym} circuit breaker: {halt}")
            continue
        except Exception as e:
            log.warning(f"  ✗ {sym} gate failed: {e}")
            continue

        result = broker.buy(
            sym, dollar_amount=amount,
            stop_loss_pct=config.GAPFILL_STOP_PCT,
            take_profit_pct=None,
        )
        if result.get("blocked"):
            log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
            continue
        if not result.get("stop_attached"):
            broker.sell(sym, qty=result["qty"])
            log.info(f"  ✗ {sym} stop-attach failed — flattened {result['qty']} sh")
            continue

        log.info(f"  ✓ GapFill {sym} {result['qty']} sh @ ${result['price']:.2f} "
                 f"SL=${result['stop']} target=${c['target']:.2f}")
        trade_logger.log_event("order_placed", "gapfill", sym,
                               qty=result["qty"], price=result["price"],
                               stop=result["stop"], target=c["target"],
                               amount=round(amount, 2), gap_pct=c["gap_pct"])
        _mark_bought(sym, result)
        pead_track(sym, result["price"], surprise_pct=c["gap_pct"],
                   report_date=datetime.date.today().isoformat(),
                   strategy="gapfill", hold_days=1)
        slots[0] -= 1


# ── Momentum Continuation runner ───────────────────────────────────────────
def _run_momentum(broker, cb, pv, slots, held, already_bought_today, sector_counts):
    """Momentum: ride 3-5 day winning streaks. Win rate 55-65%."""
    if screen_momentum is None:
        log.warning("Momentum: screener not loaded — skip")
        return
    log.info("Momentum: screening 3-day streaks...")
    candidates = screen_momentum()
    log.info(f"Momentum: {len(candidates)} candidates")
    if not candidates:
        return
    for c in candidates:
        sym = c["symbol"]
        if slots[0] <= 0:
            log.info("Slots exhausted — Momentum stopping")
            break
        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            continue

        # News filter — skip if pre_market research flagged bad sentiment
        _mnews = _today_brief.get("stock_news", {}).get(sym, {})
        if _mnews.get("skip"):
            log.info(f"  ✗ {sym} SKIP — news risk: {_mnews.get('reason', 'flagged by research')}")
            trade_logger.log_event("order_skipped", "momentum", sym,
                                   gate="news_filter", reason=_mnews.get("reason", ""))
            continue

        fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, fkp, "momentum", log):
            continue

        amount = pv * 0.03
        log.info(f"Momentum BUY {sym} | {c['streak_days']}d streak "
                 f"+{c['momentum_pct']}% RV={c['rel_volume']}x score={c['score']}")
        trade_logger.log_event("signal_detected", "momentum", sym,
                               price=c["price"], streak_days=c["streak_days"],
                               momentum_pct=c["momentum_pct"], rel_volume=c["rel_volume"],
                               score=c["score"])
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                continue
            cb.check_before_order(intended_notional=amount, symbol=sym)
        except EmergencyLiquidation as emerg:
            log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
            raise
        except TradingHalted as halt:
            log.warning(f"  ✗ {sym} circuit breaker: {halt}")
            continue
        except Exception as e:
            log.warning(f"  ✗ {sym} gate failed: {e}")
            continue

        result = broker.buy(
            sym, dollar_amount=amount,
            stop_loss_pct=config.MOMENTUM_STOP_PCT,
            take_profit_pct=config.MOMENTUM_TAKE_PROFIT_PCT,
        )
        if result.get("blocked"):
            log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
            continue
        if not result.get("stop_attached"):
            broker.sell(sym, qty=result["qty"])
            log.info(f"  ✗ {sym} stop-attach failed — flattened {result['qty']} sh")
            continue

        log.info(f"  ✓ Momentum {sym} {result['qty']} sh @ ${result['price']:.2f} "
                 f"SL=${result['stop']} TP=${result['target']}")
        trade_logger.log_event("order_placed", "momentum", sym,
                               qty=result["qty"], price=result["price"],
                               stop=result["stop"], target=result["target"],
                               amount=round(amount, 2))
        _mark_bought(sym, result)
        pead_track(sym, result["price"], surprise_pct=c["score"],
                   report_date=datetime.date.today().isoformat(),
                   strategy="momentum", hold_days=c["hold_days"])
        slots[0] -= 1


# ── Sector Rotation runner ─────────────────────────────────────────────────
def _run_sector(broker, cb, pv, slots, held, already_bought_today, sector_counts):
    """Sector Rotation: buy leaders in top-performing sectors. Hold 14d."""
    if screen_sector is None:
        log.warning("Sector: screener not loaded — skip")
        return
    log.info("Sector Rotation: screening sector leaders...")
    candidates = screen_sector()
    log.info(f"Sector Rotation: {len(candidates)} candidates")
    if not candidates:
        return
    for c in candidates:
        sym = c["symbol"]
        if slots[0] <= 0:
            log.info("Slots exhausted — Sector stopping")
            break
        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            continue

        # Sector rotation is inherently sector-aware — don't double-check
        amount = pv * config.MAX_POSITION_SIZE_PCT
        log.info(f"Sector BUY {sym} [{c['sector']}] | "
                 f"stock+{c['stock_ret']}% sector+{c['sector_ret']}% "
                 f"RS={c['rs']} score={c['score']}")
        trade_logger.log_event("signal_detected", "sector", sym,
                               price=c["price"], sector=c["sector"],
                               sector_ret=c["sector_ret"], stock_ret=c["stock_ret"],
                               rs=c["rs"], score=c["score"])
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                continue
            cb.check_before_order(intended_notional=amount, symbol=sym)
        except EmergencyLiquidation as emerg:
            log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
            raise
        except TradingHalted as halt:
            log.warning(f"  ✗ {sym} circuit breaker: {halt}")
            continue
        except Exception as e:
            log.warning(f"  ✗ {sym} gate failed: {e}")
            continue

        result = broker.buy(
            sym, dollar_amount=amount,
            stop_loss_pct=config.SECTOR_STOP_PCT,
            take_profit_pct=config.SECTOR_TAKE_PROFIT_PCT,
        )
        if result.get("blocked"):
            log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
            continue
        if not result.get("stop_attached"):
            broker.sell(sym, qty=result["qty"])
            log.info(f"  ✗ {sym} stop-attach failed — flattened {result['qty']} sh")
            continue

        log.info(f"  ✓ Sector {sym} {result['qty']} sh @ ${result['price']:.2f} "
                 f"SL=${result['stop']} TP=${result['target']} [{c['sector']}]")
        trade_logger.log_event("order_placed", "sector", sym,
                               qty=result["qty"], price=result["price"],
                               stop=result["stop"], target=result["target"],
                               amount=round(amount, 2), sector=c["sector"])
        _mark_bought(sym, result)
        pead_track(sym, result["price"], surprise_pct=c["score"],
                   report_date=datetime.date.today().isoformat(),
                   strategy="sector", hold_days=c["hold_days"])
        slots[0] -= 1
        # Mark sector as counted so we don't over-allocate
        sector_counts[c["sector"]] = sector_counts.get(c["sector"], 0) + 1


def _run_vcp(broker, cb, pv, slots, held, already_bought_today, sector_counts):
    """VCP: volatility-contraction breakout candidates. Prefers the pre-market
    Claude-scored watchlist (state/pre_market_watchlist.json); falls back to an
    inline technical screen (raw scores, no Claude) if the file is missing or
    stale (e.g. after a container restart mid-day)."""
    watchlist_path = os.path.join(config.STATE_DIR, "pre_market_watchlist.json")
    watchlist = None
    today = datetime.datetime.now(ET).date().isoformat()

    try:
        with open(watchlist_path) as f:
            wl = json.load(f)
        if wl.get("generated", "")[:10] == today:
            watchlist = wl
        else:
            log.info(f"VCP: watchlist stale ({wl.get('generated','?')[:10]}) — running inline screen")
    except FileNotFoundError:
        log.info("VCP: no pre_market_watchlist.json — running inline screen")
    except Exception as e:
        log.warning(f"VCP: watchlist load failed ({e}) — running inline screen")

    if watchlist is None:
        try:
            raw = screen()[:15]
            buy_list = [
                {**s,
                 "score": s.get("raw_score", s.get("score", 0)),
                 "action": "BUY",
                 "reason": f"inline screen raw={s.get('raw_score', s.get('score', 0))}"}
                for s in sorted(raw, key=lambda x: x.get("raw_score", x.get("score", 0)), reverse=True)
                if s.get("raw_score", s.get("score", 0)) >= 50
            ]
            watchlist = {"buy_list": buy_list, "generated": datetime.datetime.now(ET).isoformat()}
            try:
                with open(watchlist_path, "w") as f:
                    json.dump(watchlist, f, indent=2)
            except Exception:
                pass
            log.info(f"VCP: inline screen → {len(buy_list)} BUY candidates (raw score >= 50)")
        except Exception as e:
            log.error(f"VCP: inline screen failed ({e}) — skipping")
            return

    candidates = watchlist.get("buy_list", [])
    log.info(f"VCP: {len(candidates)} candidates from this morning's screen")
    if not candidates:
        return

    for c in candidates:
        sym = c["symbol"]
        size_pct = config.VCP_SIZE_PCT
        amount = pv * size_pct

        trade_logger.log_event(
            "signal_detected", "vcp", sym,
            score=c.get("score"), reason=c.get("reason", ""),
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event("order_skipped", "vcp", sym,
                                   gate="already_held", reason="already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event("order_skipped", "vcp", sym,
                                   gate="idempotency", reason="already bought today")
            continue

        _news = _today_brief.get("stock_news", {}).get(sym, {})
        if _news.get("skip"):
            log.info(f"  ✗ {sym} SKIP — news risk: {_news.get('reason', 'flagged by research')}")
            trade_logger.log_event("order_skipped", "vcp", sym,
                                   gate="news_filter", reason=_news.get("reason", ""))
            continue

        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "vcp", log):
            continue

        log.info(f"VCP BUY {sym} | score={c.get('score')} | {str(c.get('reason', ''))[:60]} | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash from SPY base")
                trade_logger.log_event("gate_failed", "vcp", sym,
                                       gate="free_cash", amount=round(amount, 2),
                                       reason="cannot free cash from SPY base")
                continue
            trade_logger.log_event("gate_passed", "vcp", sym,
                                   gate="free_cash", amount=round(amount, 2))
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event("gate_passed", "vcp", sym,
                                       gate="circuit_breaker", amount=round(amount, 2))
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event("gate_failed", "vcp", sym,
                                       gate="emergency_liquidation", reason=str(emerg))
                raise
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} blocked by circuit breaker: {halt}")
                trade_logger.log_event("gate_failed", "vcp", sym,
                                       gate="circuit_breaker", reason=str(halt))
                continue
            result = broker.buy(
                sym, dollar_amount=amount,
                stop_loss_pct=config.VCP_STOP_PCT,
                take_profit_pct=None,
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event("order_skipped", "vcp", sym,
                                       gate="broker_buy", reason=result.get("reason"))
                continue
            if not result.get("stop_attached"):
                log.error(f"  ✗ {sym} stop NOT attached — flattening")
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event("order_skipped", "vcp", sym,
                                       gate="stop_attach", reason="stop-loss attach failed — flattened",
                                       qty=result["qty"], price=result["price"])
                continue

            log.info(f"  ✓ VCP {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.VCP_HOLD_DAYS}d)")
            trade_logger.log_event("order_placed", "vcp", sym,
                                   qty=result["qty"], price=result["price"],
                                   stop=result["stop"], amount=round(amount, 2),
                                   hold_days=config.VCP_HOLD_DAYS)

            pead_track(sym, result["price"],
                       surprise_pct=c.get("score", 0),
                       report_date=datetime.date.today().isoformat(),
                       strategy="vcp",
                       hold_days=config.VCP_HOLD_DAYS)
            send_trade_alert(
                action="BUY", ticker=sym, shares=result["qty"],
                price=result["price"], stop=result["stop"], target=None,
                reason=f"VCP score={c.get('score')} {str(c.get('reason', ''))[:80]}",
            )
            _mark_bought(sym, result)
            _append_trade_log({
                "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                "symbol": sym, "side": "buy", "qty": result.get("qty"),
                "price": result.get("price"), "stop": result.get("stop"),
                "target": None, "strategy": "vcp",
                "score": c.get("score"),
                "exit_date": None, "exit_price": None, "pnl_pct": None,
            })
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — VCP stopping")
                break
        except Exception as e:
            log.error(f"  ✗ VCP {sym} failed: {e}")


def _run_crypto(broker, cb, pv, slots, held, already_bought_today, sector_counts):
    """Crypto momentum: buy BTC/USD, ETH/USD, SOL/USD on 24h breakout."""
    from core.crypto_screener import screen as crypto_screen
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    import time as _time

    log.info("Crypto: screening BTC/ETH/SOL for 24h momentum...")
    candidates = crypto_screen()
    log.info(f"Crypto: {len(candidates)} momentum candidates")
    if not candidates:
        return

    size_pct = config.MAX_POSITION_SIZE_PCT

    for c in candidates:
        sym = c["symbol"]
        amount = pv * size_pct

        if sym in held or sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already holding/bought today")
            continue

        log.info(f"Crypto BUY {sym} | momentum={c['momentum_pct']:+.1f}% | vol×{c['vol_ratio']:.1f} | ${amount:,.0f}")
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash from SPY base")
                continue
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                raise
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} blocked by circuit breaker: {halt}")
                continue

            notional = round(min(amount, broker.buying_power()), 2)
            if notional < 1.0:
                log.warning(f"  ✗ {sym} SKIP — notional ${notional:.2f} below $1 minimum")
                continue

            order = broker.trade.submit_order(MarketOrderRequest(
                symbol=sym,
                notional=notional,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
            ))
            log.info(f"Crypto BUY {sym} ${notional:.2f} notional submitted [{str(order.id)[:8]}]")

            fill_price = None
            filled_qty = 0.0
            for _ in range(10):
                try:
                    o = broker.trade.get_order_by_id(order.id)
                    if o.filled_avg_price:
                        fill_price = float(o.filled_avg_price)
                        filled_qty = float(o.filled_qty) if o.filled_qty else round(notional / fill_price, 9)
                        break
                except Exception:
                    pass
                _time.sleep(0.5)

            basis = fill_price or c["price"]
            if filled_qty <= 0:
                filled_qty = round(notional / basis, 9)

            stop = round(basis * (1 - config.VCP_STOP_PCT), 2)
            stop_attached, _ = broker.attach_stop_target(sym, filled_qty, stop, None)

            log.info(f"  ✓ Crypto {sym} {filled_qty:.6f} @ ${basis:,.2f} SL=${stop:,.2f} stop_attached={stop_attached}")
            send_trade_alert(
                action="BUY",
                ticker=sym.replace("/USD", ""),
                shares=round(filled_qty, 6),
                price=basis,
                stop=stop,
                target=None,
                reason=f"Crypto momentum {c['momentum_pct']:+.1f}% vol×{c['vol_ratio']:.1f}",
            )
            _mark_bought(sym, {"qty": filled_qty, "price": basis})
            _append_trade_log({
                "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                "symbol": sym, "side": "buy", "qty": filled_qty,
                "price": basis, "stop": stop, "target": None, "strategy": "crypto",
                "score": c.get("score"), "exit_date": None, "exit_price": None, "pnl_pct": None,
            })
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — crypto stopping")
                break
        except Exception as e:
            log.error(f"  ✗ Crypto {sym} failed: {e}")


# Strategy dispatch table, keyed by the values accepted in STRATEGY_MODE
# (core/config.py). Module-level so it's inspectable/testable without calling
# run(); run() iterates config.STRATEGY_MODES against this map in order.
STRATEGY_HANDLERS = {
    "pead":     _run_pead,
    "meanrev":  _run_meanrev,
    "insider":  _run_insider,
    "squeeze":  _run_squeeze,
    "breakout": _run_breakout,
    "earnmom":  _run_earnmom,
    "gapfill":  _run_gapfill,
    "momentum": _run_momentum,
    "sector":   _run_sector,
    "vcp":      _run_vcp,
    "crypto":   _run_crypto,
}


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

    # Circuit-breaker daily-loss check via the unified CircuitBreaker instance (Fix 7)
    equity_now = float(broker.get_account().equity)
    day_pnl = (equity_now - day_start) / day_start * 100
    if day_pnl <= -cb.max_daily_loss * 100:
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

    # ── Load today's research brief (built by pre_market at 6 AM) ────────────
    global _today_brief
    try:
        from core.researcher import load_today_brief
        _today_brief = load_today_brief()
        if _today_brief:
            log.info("Research brief: risk=%s | %s",
                     _today_brief.get("macro_risk", "?"),
                     _today_brief.get("summary", "")[:80])
            if _today_brief.get("trade_bias_override") == "cash":
                _evt = (_today_brief.get("event_blocks") or [{}])[0]
                log.warning("RESEARCH OVERRIDE: CASH — %s",
                            _evt.get("event", "high-impact event today"))
                return
        else:
            log.info("No research brief found — proceeding without news filter")
    except Exception as _be:
        log.warning("Research brief load failed (non-fatal): %s", _be)

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
        log.warning("Regime gate SKIPPED: no SPY bars available.")
        # Proceed with NEUTRAL regime so strategy still runs, but log explicitly
        log.info("Regime: fallback NEUTRAL (SPY bars unavailable)")

    # Emergency liquidation check before strategy loop
    if cb.liquidation_required():
        log.error(f"EMERGENCY LIQUIDATION: equity ${equity_now:,.2f} vs day-start ${day_start:,.2f} ({day_pnl:+.2f}%)")
        try:
            broker.cancel_all_orders()
            positions = broker.get_positions()
            for p in positions:
                if is_base_symbol(p.symbol): continue
                try:
                    broker.close_position(p.symbol)
                    log.info(f"  Emergency closed {p.symbol}")
                except Exception as e:
                    log.warning(f"  Emergency close {p.symbol} failed: {e}")
        except Exception as e:
            log.error(f"Emergency liquidation attempt failed: {e}")
        send_trade_alert(action="EMERGENCY", ticker="ALL", shares=0, price=0,
                         stop=0, target=0,
                         reason="Emergency liquidation: emergency threshold breached")
        trade_logger.log_event("emergency_liquidation", "all", None)
        logger.banner(log, "EMERGENCY LIQUIDATION — NO STRATEGIES RUN")
        return

    # Build initial sector counts from held positions (FMP lookup)
    fmp_key = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
    sector_counts = _build_sector_counts(broker, fmp_key)
    if sector_counts:
        log.info(f"Sector snapshot: {sector_counts}")

    # ── STRATEGY ROUTER ───────────────────────────────────────────────────────
    # Each runner consumes from the shared `slots` pool. Held-set and
    # already_bought_today accumulate across runners so the same symbol is
    # never double-bought within a single run. Handler map: STRATEGY_HANDLERS
    # (module level, defined near the runners above).
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
            handler(broker, cb, pv, slots, held, already_bought_today, sector_counts)
        except EmergencyLiquidation:
            raise  # propagate to outer handler
        except TradingHalted:
            pass  # already logged per-symbol in runner
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
