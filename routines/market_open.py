from __future__ import annotations

"""
MARKET-OPEN ROUTINE — 9:30 AM ET, Mon-Fri
FULL SKILLS, ALPACA-ONLY (no FMP = no rate limits).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
import json

import pytz

from circuit_breaker import CircuitBreaker, EmergencyLiquidation, TradingHalted
from core import config, cost_tracker, logger, timeseries_signal, trade_logger
from core.broker import BrokerClient
from core.buffett_tracker import add_position as buffett_track
from core.buffett_tracker import remove_position as buffett_untrack
from core.buffett_value import get_top_buy_signals, screen_for_buffett_candidates
from core.earnings_screener import screen_earnings
from core.notifier import send_trade_alert
from core.pead_tracker import add_position as pead_track
from core.protection import reattach_missing_protection
from core.screener import fetch_bars, screen
from core.spy_base import free_cash_for_pead, is_base_symbol, rebalance_to_spy
from core.spy_base import log_status as spy_log
from regime_gate import classify

log = logger.setup("market_open")

from functools import lru_cache

import requests as _req  # noqa: F401

# ── SECTOR CONCENTRATION GUARD helpers ──────────────────────────────────────
# MAX_PER_SECTOR enforced across all strategies within a single run
_SECTOR_CACHE: dict = {}


@lru_cache(maxsize=500)
def _fetch_symbol_sector(symbol: str, api_key: str) -> str | None:
    """Look up GICS sector via yfinance info. Cached in-process."""
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        sector = ticker.info.get("sector")
        if sector:
            return sector.strip()
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
        sector = getattr(p, "sector", None) or _fetch_symbol_sector(p.symbol, fmp_key)
        if sector:
            counts[sector] += 1
    return dict(counts)


def _sector_gate(symbol: str, sector_counts: dict, fmp_key: str, strategy: str, log) -> bool:
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
            "gate_failed",
            strategy,
            symbol,
            gate="sector_concentration",
            reason=f"sector {sector} at {current}/{max_per}",
            sector=sector,
            current=current,
            cap=max_per,
        )
        return False
    sector_counts[sector] = current + 1
    return True


def _timeseries_gate(symbol: str, strategy: str, log) -> bool:
    """Confirming filter: an existing strategy's entry only proceeds if the
    time-series directional forecast (core.timeseries_signal) agrees with
    "long" or is neutral/inconclusive. No-op (returns True) when
    TIMESERIES_ENABLED is False (the default until it clears its own
    standalone backtest — see core/config.py).

    Unlike _sector_gate/spread checks, a fetch/model failure here defaults
    to ALLOW, not block: this is an optional secondary confirmation on top
    of an already-validated strategy, not a safety check on the order
    itself — erroring toward "no opinion" can't introduce a new failure
    mode into a strategy that already passed its own validation bar.
    """
    if not getattr(config, "TIMESERIES_ENABLED", False):
        return True
    try:
        bars = fetch_bars([symbol], days=config.TIMESERIES_MIN_HISTORY_DAYS * 2).get(symbol, [])
        result = timeseries_signal.confirms(
            "long", bars, min_confidence=config.TIMESERIES_MIN_CONFIDENCE
        )
    except Exception as e:
        log.warning(
            "  timeseries gate %s: fetch/forecast failed (%s) — defaulting to allow", symbol, e
        )
        return True
    if not result["allowed"]:
        log.info(
            f"  SKIP {symbol} — timeseries model disagrees: "
            f"direction={result['direction']} confidence={result['confidence']:.2f}"
        )
        trade_logger.log_event(
            "gate_failed",
            strategy,
            symbol,
            gate="timeseries_confirm",
            reason=f"model predicts {result['direction']} (confidence={result['confidence']:.2f})",
            direction=result["direction"],
            confidence=result["confidence"],
        )
    return result["allowed"]


def _affordable_candidates(broker, candidates: list, strategy: str, log) -> list:
    """Drop candidates the account genuinely cannot buy any of, using each
    candidate's own affordable_budget() (which nets out any existing
    position in that symbol). Screeners rank by signal strength, not price —
    on a small account the top-ranked names are often priced above a single
    whole share, so this must run BEFORE any candidates[:slots] truncation,
    or an expensive top candidate silently crowds out a cheaper one further
    down the list that the account could actually buy.

    A candidate whose budget can't cover even 1 whole share is dropped here.
    broker.buy() no longer falls back to a fractional (notional) order for
    those — fractional positions can only ever get a DAY-tif stop from
    Alpaca (GTC is rejected), so protection lapses at that day's close —
    so there is no point carrying a whole-share-unaffordable candidate
    through to a buy() call that will just block it.
    """
    affordable = []
    for c in candidates:
        sym = c["symbol"]
        price = c.get("price", 0)
        budget = broker.affordable_budget(sym)
        if price <= 0 or budget < price:
            log.info(f"  ✗ {sym} SKIP — affordable budget ${budget:.2f} can't cover 1 whole share @ ${price:.2f}")
            trade_logger.log_event(
                "order_skipped",
                strategy,
                sym,
                gate="affordability",
                reason=f"budget ${budget:.2f} < whole-share price ${price:.2f}",
                price=price,
                budget=round(budget, 2),
            )
            continue
        affordable.append(c)
    return affordable


ET = pytz.timezone("America/New_York")

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
try:
    from core.macross_screener import screen as screen_macross
except Exception as e:
    log.error("MACross screener import failed: %s", e)
    screen_macross = None


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
MAX_BUYS = 3


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

    from alpaca.trading.enums import OrderSide, QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

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
        rec["exit_date"] = exit_info["exit_date"]
        rec["pnl_pct"] = round(pnl_pct, 4)
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
            json.dump(
                {
                    "date": today,
                    "symbols": sorted(bought),
                    "orders": [{"symbol": s, "order_id": None} for s in sorted(bought)],
                },
                f,
                indent=2,
            )
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
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    earliest = open_t + datetime.timedelta(minutes=config.ENTRY_DELAY_MIN)
    close_t = now.replace(hour=15, minute=45, second=0, microsecond=0)
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
        max_price=config.PEAD_MAX_PRICE,
        min_avg_volume=config.PEAD_MIN_AVG_VOLUME,
    )
    log.info(f"PEAD: {len(candidates)} candidates with surprise >= {config.PEAD_MIN_SURPRISE_PCT}%")

    if not candidates:
        log.info("PEAD: no earnings beats — done")
        return

    for c in candidates:
        log.info(
            f"  • {c['symbol']} surprise={c['surprise_pct']:+.1f}% "
            f"EPS={c.get('actual_eps')}/{c.get('estimated_eps')} "
            f"reported={c['report_date']} price=${c.get('price', 0):.2f}"
        )
        trade_logger.log_event(
            "signal_detected",
            "pead",
            c["symbol"],
            surprise_pct=c["surprise_pct"],
            report_date=c["report_date"],
            price=c.get("price", 0),
            actual_eps=c.get("actual_eps"),
            estimated_eps=c.get("estimated_eps"),
        )

    candidates = _affordable_candidates(broker, candidates, "pead", log)
    if not candidates:
        log.info("PEAD: no affordable candidates — done")
        return

    buys_taken = 0
    for c in candidates[: slots[0]]:
        sym = c["symbol"]
        surprise = c["surprise_pct"]

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event(
                "order_skipped", "pead", sym, gate="already_held", reason="already holding"
            )
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event(
                "order_skipped", "pead", sym, gate="idempotency", reason="already bought today"
            )
            continue

        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "pead", log):
            continue

        size_pct = config.PEAD_SIZE_PCT
        amount = pv * size_pct

        log.info(
            f"PEAD BUY {sym} | surprise={surprise:+.1f}% | "
            f"size={size_pct * 100:.0f}% | ${amount:,.0f}"
        )
        try:
            # Free SPY cash if needed for this PEAD entry
            if not free_cash_for_pead(broker, amount):
                log.warning(f"✗ {sym} SKIP — cannot free ${amount:,.0f} from SPY base")
                trade_logger.log_event(
                    "gate_failed",
                    "pead",
                    sym,
                    gate="free_cash",
                    amount=round(amount, 2),
                    reason="cannot free cash from SPY base",
                )
                continue
            trade_logger.log_event(
                "gate_passed", "pead", sym, gate="free_cash", amount=round(amount, 2)
            )

            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event(
                    "gate_passed", "pead", sym, gate="circuit_breaker", amount=round(amount, 2)
                )
            except EmergencyLiquidation as emerg:
                log.error(f"✗ EMERGENCY LIQUIDATION — circuit breaker: {emerg}")
                trade_logger.log_event(
                    "gate_failed", "pead", sym, gate="emergency_liquidation", reason=str(emerg)
                )
                # Propagate so market_open can close all positions before returning
                raise
            except TradingHalted as halt:
                log.warning(f"✗ {sym} blocked by circuit breaker: {halt}")
                trade_logger.log_event(
                    "gate_failed", "pead", sym, gate="circuit_breaker", reason=str(halt)
                )
                continue

            # PEAD uses wide stop (-15%), NO take-profit (time exit at 60d)
            # Set take_profit very wide (99%) so OCO doesn't trigger early
            result = broker.buy(
                sym,
                dollar_amount=amount,
                stop_loss_pct=config.PEAD_STOP_PCT,
                take_profit_pct=None,  # no hard target (time-managed 60d exit)
                strategy="pead",
            )
            if result.get("blocked"):
                log.warning(f"✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event(
                    "order_skipped", "pead", sym, gate="broker_buy", reason=result.get("reason")
                )
                continue
            if not result.get("stop_attached"):
                log.error(f"✗ {sym} bought but stop NOT attached — flattening")
                broker.sell(sym, qty=result["qty"])
                send_trade_alert(
                    action="FLATTEN",
                    ticker=sym,
                    shares=result["qty"],
                    price=result["price"],
                    stop=result.get("stop", 0),
                    target=result.get("target", 0),
                    reason="PEAD stop-loss attach failed — position rejected",
                )
                trade_logger.log_event(
                    "order_skipped",
                    "pead",
                    sym,
                    gate="stop_attach",
                    reason="stop-loss attach failed — flattened",
                    qty=result["qty"],
                    price=result["price"],
                )
                continue

            log.info(
                f"✓ PEAD {sym} {result['qty']} sh @ ${result['price']:.2f} "
                f"SL={result['stop']} (hold {config.PEAD_HOLD_DAYS}d)"
            )
            trade_logger.log_event(
                "order_placed",
                "pead",
                sym,
                qty=result["qty"],
                price=result["price"],
                stop=result["stop"],
                surprise_pct=surprise,
                amount=round(amount, 2),
                hold_days=config.PEAD_HOLD_DAYS,
            )

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
            _append_trade_log(
                {
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
                }
            )
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

    candidates = _affordable_candidates(broker, candidates, "meanrev", log)
    if not candidates:
        log.info("MeanRev: no affordable candidates — done")
        return

    for c in candidates:
        sym = c["symbol"]
        price = c["price"]
        size_pct = config.MEANREV_SIZE_PCT
        amount = pv * size_pct

        trade_logger.log_event(
            "signal_detected",
            "meanrev",
            sym,
            rsi=c["rsi"],
            bb_position=c["bb_position"],
            momentum_pct=c.get("momentum_pct", 0),
            price=price,
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event(
                "order_skipped", "meanrev", sym, gate="already_held", reason="already holding"
            )
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event(
                "order_skipped", "meanrev", sym, gate="idempotency", reason="already bought today"
            )
            continue

        # News filter — skip if pre_market research flagged bad sentiment
        _news = _today_brief.get("stock_news", {}).get(sym, {})
        if _news.get("skip"):
            log.info(f"  ✗ {sym} SKIP — news risk: {_news.get('reason', 'flagged by research')}")
            trade_logger.log_event(
                "order_skipped", "meanrev", sym, gate="news_filter", reason=_news.get("reason", "")
            )
            continue

        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "meanrev", log):
            continue
        if not _timeseries_gate(sym, "meanrev", log):
            continue

        log.info(
            f"MeanRev BUY {sym} | RSI={c['rsi']} BBpos={c['bb_position']:.0f}% "
            f"momentum={c.get('momentum_pct', 0):+.1f}% | ${amount:,.0f}"
        )
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash from SPY base")
                trade_logger.log_event(
                    "gate_failed",
                    "meanrev",
                    sym,
                    gate="free_cash",
                    amount=round(amount, 2),
                    reason="cannot free cash from SPY base",
                )
                continue
            trade_logger.log_event(
                "gate_passed", "meanrev", sym, gate="free_cash", amount=round(amount, 2)
            )
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event(
                    "gate_passed", "meanrev", sym, gate="circuit_breaker", amount=round(amount, 2)
                )
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} blocked by circuit breaker: {halt}")
                trade_logger.log_event(
                    "gate_failed", "meanrev", sym, gate="circuit_breaker", reason=str(halt)
                )
                continue
            result = broker.buy(
                sym,
                dollar_amount=amount,
                stop_loss_pct=config.MEANREV_STOP_PCT,
                take_profit_pct=None,  # no hard target (time-managed exit)
                strategy="meanrev",
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event(
                    "order_skipped", "meanrev", sym, gate="broker_buy", reason=result.get("reason")
                )
                continue
            if not result.get("stop_attached"):
                log.error(f"  ✗ {sym} stop NOT attached — flattening")
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event(
                    "order_skipped",
                    "meanrev",
                    sym,
                    gate="stop_attach",
                    reason="stop-loss attach failed — flattened",
                    qty=result["qty"],
                    price=result["price"],
                )
                continue

            log.info(
                f"  ✓ MeanRev {sym} {result['qty']} sh @ ${result['price']:.2f} "
                f"SL={result['stop']} (hold {config.MEANREV_HOLD_DAYS}d)"
            )
            trade_logger.log_event(
                "order_placed",
                "meanrev",
                sym,
                qty=result["qty"],
                price=result["price"],
                stop=result["stop"],
                amount=round(amount, 2),
                hold_days=config.MEANREV_HOLD_DAYS,
            )

            pead_track(
                sym,
                result["price"],
                surprise_pct=c.get("rsi", 0),
                report_date=datetime.date.today().isoformat(),
                strategy="meanrev",
                hold_days=config.MEANREV_HOLD_DAYS,
            )
            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=None,
                reason=(
                    f"MeanRev RSI={c['rsi']} BB={c['bb_position']:.0f}%"
                    f" momentum={c.get('momentum_pct', 0):+.1f}%"
                ),
            )
            _mark_bought(sym, result)
            _append_trade_log(
                {
                    "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                    "symbol": sym,
                    "side": "buy",
                    "qty": result.get("qty"),
                    "price": result.get("price"),
                    "stop": result.get("stop"),
                    "target": None,
                    "strategy": "meanrev",
                    "rsi": c["rsi"],
                    "bb_position": c["bb_position"],
                    "exit_date": None,
                    "exit_price": None,
                    "pnl_pct": None,
                }
            )
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

    candidates = _affordable_candidates(broker, candidates, "insider", log)
    if not candidates:
        log.info("Insider: no affordable candidates — done")
        return

    for c in candidates:
        sym = c["symbol"]
        size_pct = config.INSIDER_SIZE_PCT
        amount = pv * size_pct

        trade_logger.log_event(
            "signal_detected",
            "insider",
            sym,
            insider_score=c["insider_score"],
            n_transactions=c["n_transactions"],
            total_dollar=c["total_dollar"],
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event(
                "order_skipped", "insider", sym, gate="already_held", reason="already holding"
            )
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event(
                "order_skipped", "insider", sym, gate="idempotency", reason="already bought today"
            )
            continue

        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "insider", log):
            continue
        if not _timeseries_gate(sym, "insider", log):
            continue

        log.info(
            f"Insider BUY {sym} | score={c['insider_score']:.0f} "
            f"txns={c['n_transactions']} total=${c['total_dollar']:,.0f} | ${amount:,.0f}"
        )
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                trade_logger.log_event(
                    "gate_failed",
                    "insider",
                    sym,
                    gate="free_cash",
                    amount=round(amount, 2),
                    reason="cannot free cash from SPY base",
                )
                continue
            trade_logger.log_event(
                "gate_passed", "insider", sym, gate="free_cash", amount=round(amount, 2)
            )
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event(
                    "gate_passed", "insider", sym, gate="circuit_breaker", amount=round(amount, 2)
                )
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event(
                    "gate_failed", "insider", sym, gate="emergency_liquidation", reason=str(emerg)
                )
                raise
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                trade_logger.log_event(
                    "gate_failed", "insider", sym, gate="circuit_breaker", reason=str(halt)
                )
                continue
            result = broker.buy(
                sym,
                dollar_amount=amount,
                stop_loss_pct=config.INSIDER_STOP_PCT,
                take_profit_pct=config.INSIDER_TARGET_PCT,
                strategy="insider",
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event(
                    "order_skipped", "insider", sym, gate="broker_buy", reason=result.get("reason")
                )
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event(
                    "order_skipped",
                    "insider",
                    sym,
                    gate="stop_attach",
                    reason="stop-loss attach failed — flattened",
                    qty=result["qty"],
                    price=result["price"],
                )
                continue

            log.info(
                f"  ✓ Insider {sym} {result['qty']} sh @ ${result['price']:.2f} "
                f"SL={result['stop']} TP={result['target']} (hold {config.INSIDER_HOLD_DAYS}d)"
            )
            trade_logger.log_event(
                "order_placed",
                "insider",
                sym,
                qty=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=result["target"],
                amount=round(amount, 2),
                hold_days=config.INSIDER_HOLD_DAYS,
            )

            pead_track(
                sym,
                result["price"],
                surprise_pct=c.get("insider_score", 0),
                report_date=datetime.date.today().isoformat(),
                strategy="insider",
                hold_days=config.INSIDER_HOLD_DAYS,
            )
            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=result["target"],
                reason=(f"Insider score={c['insider_score']:.0f} {c['n_transactions']} purchases"),
            )
            _mark_bought(sym, result)
            _append_trade_log(
                {
                    "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                    "symbol": sym,
                    "side": "buy",
                    "qty": result.get("qty"),
                    "price": result.get("price"),
                    "stop": result.get("stop"),
                    "target": None,
                    "strategy": "insider",
                    "insider_score": c["insider_score"],
                    "total_dollar": c["total_dollar"],
                    "exit_date": None,
                    "exit_price": None,
                    "pnl_pct": None,
                }
            )
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

    candidates = _affordable_candidates(broker, candidates, "squeeze", log)
    if not candidates:
        log.info("Squeeze: no affordable candidates — done")
        return

    for c in candidates:
        sym = c["symbol"]
        size_pct = config.SQUEEZE_SIZE_PCT
        amount = pv * size_pct

        trade_logger.log_event(
            "signal_detected",
            "squeeze",
            sym,
            short_interest_pct=c["short_interest_pct"],
            days_to_cover=c["days_to_cover"],
            momentum_pct=c["momentum_pct"],
            score=c["score"],
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event(
                "order_skipped", "squeeze", sym, gate="already_held", reason="already holding"
            )
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event(
                "order_skipped", "squeeze", sym, gate="idempotency", reason="already bought today"
            )
            continue

        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "squeeze", log):
            continue

        log.info(
            f"Squeeze BUY {sym} | SI={c['short_interest_pct']:.1f}%"
            f" DTC={c['days_to_cover']:.1f}d mom={c['momentum_pct']:+.1f}% "
            f"score={c['score']:.0f} | ${amount:,.0f}"
        )
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                trade_logger.log_event(
                    "gate_failed",
                    "squeeze",
                    sym,
                    gate="free_cash",
                    amount=round(amount, 2),
                    reason="cannot free cash from SPY base",
                )
                continue
            trade_logger.log_event(
                "gate_passed", "squeeze", sym, gate="free_cash", amount=round(amount, 2)
            )
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event(
                    "gate_passed", "squeeze", sym, gate="circuit_breaker", amount=round(amount, 2)
                )
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event(
                    "gate_failed", "squeeze", sym, gate="emergency_liquidation", reason=str(emerg)
                )
                raise
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                trade_logger.log_event(
                    "gate_failed", "squeeze", sym, gate="circuit_breaker", reason=str(halt)
                )
                continue
            result = broker.buy(
                sym,
                dollar_amount=amount,
                stop_loss_pct=config.SQUEEZE_STOP_PCT,
                take_profit_pct=None,
                strategy="squeeze",
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event(
                    "order_skipped", "squeeze", sym, gate="broker_buy", reason=result.get("reason")
                )
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event(
                    "order_skipped",
                    "squeeze",
                    sym,
                    gate="stop_attach",
                    reason="stop-loss attach failed — flattened",
                    qty=result["qty"],
                    price=result["price"],
                )
                continue

            log.info(
                f"  ✓ Squeeze {sym} {result['qty']} sh @ ${result['price']:.2f} "
                f"SL={result['stop']} (hold {config.SQUEEZE_HOLD_DAYS}d)"
            )
            trade_logger.log_event(
                "order_placed",
                "squeeze",
                sym,
                qty=result["qty"],
                price=result["price"],
                stop=result["stop"],
                amount=round(amount, 2),
                hold_days=config.SQUEEZE_HOLD_DAYS,
            )

            pead_track(
                sym,
                result["price"],
                surprise_pct=c.get("score", 0),
                report_date=datetime.date.today().isoformat(),
                strategy="squeeze",
                hold_days=config.SQUEEZE_HOLD_DAYS,
            )
            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=None,
                reason=(
                    f"Squeeze SI={c['short_interest_pct']:.1f}%"
                    f" DTC={c['days_to_cover']:.1f}d mom={c['momentum_pct']:+.1f}%"
                ),
            )
            _mark_bought(sym, result)
            _append_trade_log(
                {
                    "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                    "symbol": sym,
                    "side": "buy",
                    "qty": result.get("qty"),
                    "price": result.get("price"),
                    "stop": result.get("stop"),
                    "target": None,
                    "strategy": "squeeze",
                    "si_pct": c["short_interest_pct"],
                    "days_to_cover": c["days_to_cover"],
                    "momentum_pct": c["momentum_pct"],
                    "exit_date": None,
                    "exit_price": None,
                    "pnl_pct": None,
                }
            )
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
            "signal_detected",
            "breakout",
            sym,
            price=c["price"],
            clearance_pct=c["clearance_pct"],
            volume_ratio=c["volume_ratio"],
            atr_pct=c["atr_pct"],
            score=c["score"],
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event(
                "order_skipped", "breakout", sym, gate="already_held", reason="already holding"
            )
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event(
                "order_skipped", "breakout", sym, gate="idempotency", reason="already bought today"
            )
            continue

        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "breakout", log):
            continue
        if not _timeseries_gate(sym, "breakout", log):
            continue

        log.info(
            f"Breakout BUY {sym} | price=${c['price']:.2f} "
            f"clearance={c['clearance_pct']:+.2f}% vol={c['volume_ratio']}x "
            f"ATR={c['atr_pct']:.1f}% score={c['score']:.0f} | ${amount:,.0f}"
        )
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                trade_logger.log_event(
                    "gate_failed",
                    "breakout",
                    sym,
                    gate="free_cash",
                    amount=round(amount, 2),
                    reason="cannot free cash from SPY base",
                )
                continue
            trade_logger.log_event(
                "gate_passed", "breakout", sym, gate="free_cash", amount=round(amount, 2)
            )
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event(
                    "gate_passed", "breakout", sym, gate="circuit_breaker", amount=round(amount, 2)
                )
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event(
                    "gate_failed", "breakout", sym, gate="emergency_liquidation", reason=str(emerg)
                )
                raise
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                trade_logger.log_event(
                    "gate_failed", "breakout", sym, gate="circuit_breaker", reason=str(halt)
                )
                continue
            result = broker.buy(
                sym,
                dollar_amount=amount,
                stop_loss_pct=config.BREAKOUT_STOP_PCT,
                take_profit_pct=None,
                strategy="breakout",
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event(
                    "order_skipped", "breakout", sym, gate="broker_buy", reason=result.get("reason")
                )
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event(
                    "order_skipped",
                    "breakout",
                    sym,
                    gate="stop_attach",
                    reason="stop-loss attach failed — flattened",
                    qty=result["qty"],
                    price=result["price"],
                )
                continue

            log.info(
                f"  ✓ Breakout {sym} {result['qty']} sh @ ${result['price']:.2f} "
                f"SL={result['stop']} (hold {config.BREAKOUT_HOLD_DAYS}d)"
            )
            trade_logger.log_event(
                "order_placed",
                "breakout",
                sym,
                qty=result["qty"],
                price=result["price"],
                stop=result["stop"],
                amount=round(amount, 2),
                hold_days=config.BREAKOUT_HOLD_DAYS,
            )

            pead_track(
                sym,
                result["price"],
                surprise_pct=c.get("score", 0),
                report_date=datetime.date.today().isoformat(),
                strategy="breakout",
                hold_days=config.BREAKOUT_HOLD_DAYS,
            )
            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=None,
                reason=(
                    f"Breakout clearance={c['clearance_pct']:+.2f}%"
                    f" vol={c['volume_ratio']}x ATR={c['atr_pct']:.1f}%"
                ),
            )
            _mark_bought(sym, result)
            _append_trade_log(
                {
                    "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                    "symbol": sym,
                    "side": "buy",
                    "qty": result.get("qty"),
                    "price": result.get("price"),
                    "stop": result.get("stop"),
                    "target": None,
                    "strategy": "breakout",
                    "clearance_pct": c["clearance_pct"],
                    "volume_ratio": c["volume_ratio"],
                    "atr_pct": c["atr_pct"],
                    "exit_date": None,
                    "exit_price": None,
                    "pnl_pct": None,
                }
            )
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — Breakout stopping")
                break
        except Exception as e:
            log.error(f"  ✗ Breakout {sym} failed: {e}")


def _run_buffett_value(broker, cb, pv, slots, held, already_bought_today, sector_counts):
    """Buffett Value: business quality + margin-of-safety screen, candlestick
    entry timing. NO stop-loss and NO calendar exit by design -- see
    skills/buffett-value/scripts/. Entries use broker.buy_simple() (no
    stop attached); exits are evaluated separately by
    _run_buffett_value_exits() in market_close.py, not by this handler or
    by the generic force-close/trim loop (which assumes every position has
    a stop and would misbehave against one that doesn't)."""
    log.info("Buffett Value: screening for quality/value candidates...")
    universe = [s for s in config.SP80_UNIVERSE if s.isalpha() and len(s) <= 5]
    candidates = screen_for_buffett_candidates(universe)
    log.info(f"Buffett Value: {len(candidates)} candidates passed the fundamentals screen")
    if not candidates:
        return

    buy_signals = get_top_buy_signals(candidates, limit=MAX_BUYS)
    log.info(f"Buffett Value: {len(buy_signals)} candlestick buy signals")
    if not buy_signals:
        return

    candidates_by_symbol = {c["symbol"]: c for c in candidates}

    for sig in buy_signals:
        sym = sig["symbol"]
        amount = pv * sig["position_size_pct"]  # conviction-based sizing (3-10%)

        trade_logger.log_event(
            "signal_detected",
            "buffett_value",
            sym,
            price=sig["signal_price"],
            conviction_score=sig["conviction_score"],
            patterns=",".join(sig.get("patterns_detected", [])),
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event(
                "order_skipped", "buffett_value", sym, gate="already_held", reason="already holding"
            )
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event(
                "order_skipped", "buffett_value", sym, gate="idempotency", reason="already bought today"
            )
            continue

        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "buffett_value", log):
            continue

        log.info(
            f"Buffett Value BUY {sym} | price=${sig['signal_price']:.2f} "
            f"conviction={sig['conviction_score']:.2f} | ${amount:,.0f}"
        )
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                trade_logger.log_event(
                    "gate_failed",
                    "buffett_value",
                    sym,
                    gate="free_cash",
                    amount=round(amount, 2),
                    reason="cannot free cash from SPY base",
                )
                continue
            trade_logger.log_event(
                "gate_passed", "buffett_value", sym, gate="free_cash", amount=round(amount, 2)
            )
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event(
                    "gate_passed", "buffett_value", sym, gate="circuit_breaker", amount=round(amount, 2)
                )
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event(
                    "gate_failed", "buffett_value", sym, gate="emergency_liquidation", reason=str(emerg)
                )
                raise
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                trade_logger.log_event(
                    "gate_failed", "buffett_value", sym, gate="circuit_breaker", reason=str(halt)
                )
                continue

            candidate = candidates_by_symbol.get(sym)
            if candidate is None:
                log.warning(f"  ✗ {sym} SKIP — analyst candidate snapshot not found")
                continue

            result = broker.buy_simple(sym, dollar_amount=amount, strategy="buffett_value")
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event(
                    "order_skipped", "buffett_value", sym, gate="broker_buy", reason=result.get("reason")
                )
                continue

            log.info(
                f"  ✓ Buffett Value {sym} {result['qty']} sh @ ${result['price']:.2f} "
                f"(no stop -- fundamentals/profit-target exit only)"
            )
            trade_logger.log_event(
                "order_placed",
                "buffett_value",
                sym,
                qty=result["qty"],
                price=result["price"],
                amount=round(amount, 2),
                conviction_score=sig["conviction_score"],
            )

            buffett_track(sym, result["price"], result["qty"], entry_snapshot=candidate)

            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=None,
                target=None,
                reason=(
                    f"Buffett Value conviction={sig['conviction_score']:.2f} "
                    f"patterns={','.join(sig.get('patterns_detected', []))}"
                ),
            )
            _mark_bought(sym, result)
            _append_trade_log(
                {
                    "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                    "symbol": sym,
                    "side": "buy",
                    "qty": result.get("qty"),
                    "price": result.get("price"),
                    "stop": None,
                    "target": None,
                    "strategy": "buffett_value",
                    "conviction_score": sig["conviction_score"],
                    "exit_date": None,
                    "exit_price": None,
                    "pnl_pct": None,
                }
            )
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — Buffett Value stopping")
                break
        except Exception as e:
            log.error(f"  ✗ Buffett Value {sym} failed: {e}")


def _run_macross(broker, cb, pv, slots, held, already_bought_today, sector_counts):
    """MA Crossover: 20/50d golden cross, volume-confirmed. Hold ~21d.

    Opt-in only — not in the default STRATEGY_MODE. See core/config.py's
    STRATEGY_MODES comment: unvalidated until its own standalone backtest
    clears the same bar as breakout/meanrev/earnmom.
    """
    if screen_macross is None:
        log.warning("MACross: screener not loaded — see import error above — skipping")
        return
    log.info("MACross: screening for 20/50d golden crosses...")
    candidates = screen_macross()
    log.info(f"MACross: {len(candidates)} candidates")
    if not candidates:
        return

    for c in candidates:
        sym = c["symbol"]
        size_pct = config.MACROSS_SIZE_PCT
        amount = pv * size_pct

        trade_logger.log_event(
            "signal_detected",
            "macross",
            sym,
            price=c["price"],
            sma_fast=c["sma_fast"],
            sma_slow=c["sma_slow"],
            days_since_cross=c["days_since_cross"],
            volume_ratio=c["volume_ratio"],
            score=c["score"],
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event(
                "order_skipped", "macross", sym, gate="already_held", reason="already holding"
            )
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event(
                "order_skipped", "macross", sym, gate="idempotency", reason="already bought today"
            )
            continue

        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "macross", log):
            continue

        log.info(
            f"MACross BUY {sym} | price=${c['price']:.2f} "
            f"cross {c['days_since_cross']}d ago vol={c['volume_ratio']}x "
            f"score={c['score']:.0f} | ${amount:,.0f}"
        )
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                trade_logger.log_event(
                    "gate_failed",
                    "macross",
                    sym,
                    gate="free_cash",
                    amount=round(amount, 2),
                    reason="cannot free cash from SPY base",
                )
                continue
            trade_logger.log_event(
                "gate_passed", "macross", sym, gate="free_cash", amount=round(amount, 2)
            )
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event(
                    "gate_passed", "macross", sym, gate="circuit_breaker", amount=round(amount, 2)
                )
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event(
                    "gate_failed", "macross", sym, gate="emergency_liquidation", reason=str(emerg)
                )
                raise
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                trade_logger.log_event(
                    "gate_failed", "macross", sym, gate="circuit_breaker", reason=str(halt)
                )
                continue
            result = broker.buy(
                sym,
                dollar_amount=amount,
                stop_loss_pct=config.MACROSS_STOP_PCT,
                take_profit_pct=None,
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event(
                    "order_skipped", "macross", sym, gate="broker_buy", reason=result.get("reason")
                )
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event(
                    "order_skipped",
                    "macross",
                    sym,
                    gate="stop_attach",
                    reason="stop-loss attach failed — flattened",
                    qty=result["qty"],
                    price=result["price"],
                )
                continue

            log.info(
                f"  ✓ MACross {sym} {result['qty']} sh @ ${result['price']:.2f} "
                f"SL={result['stop']} (hold {config.MACROSS_HOLD_DAYS}d)"
            )
            trade_logger.log_event(
                "order_placed",
                "macross",
                sym,
                qty=result["qty"],
                price=result["price"],
                stop=result["stop"],
                amount=round(amount, 2),
                hold_days=config.MACROSS_HOLD_DAYS,
            )

            pead_track(
                sym,
                result["price"],
                surprise_pct=c.get("score", 0),
                report_date=datetime.date.today().isoformat(),
                strategy="macross",
                hold_days=config.MACROSS_HOLD_DAYS,
            )
            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=None,
                reason=(
                    f"MACross {config.MACROSS_FAST_PERIOD}/{config.MACROSS_SLOW_PERIOD}d "
                    f"cross {c['days_since_cross']}d ago, vol={c['volume_ratio']}x"
                ),
            )
            _mark_bought(sym, result)
            _append_trade_log(
                {
                    "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                    "symbol": sym,
                    "side": "buy",
                    "qty": result.get("qty"),
                    "price": result.get("price"),
                    "stop": result.get("stop"),
                    "target": None,
                    "strategy": "macross",
                    "sma_fast": c["sma_fast"],
                    "sma_slow": c["sma_slow"],
                    "days_since_cross": c["days_since_cross"],
                    "volume_ratio": c["volume_ratio"],
                    "exit_date": None,
                    "exit_price": None,
                    "pnl_pct": None,
                }
            )
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — MACross stopping")
                break
        except Exception as e:
            log.error(f"  ✗ MACross {sym} failed: {e}")


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
            "signal_detected",
            "earnmom",
            sym,
            surprise_pct=c["surprise_pct"],
            age_days=c["age_days"],
            drift_pct=c["drift_pct"],
            score=c["score"],
            report_date=c.get("report_date"),
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event(
                "order_skipped", "earnmom", sym, gate="already_held", reason="already holding"
            )
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event(
                "order_skipped", "earnmom", sym, gate="idempotency", reason="already bought today"
            )
            continue

        # Sector concentration guard
        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "earnmom", log):
            continue
        if not _timeseries_gate(sym, "earnmom", log):
            continue

        log.info(
            f"EarnMom BUY {sym} | surprise={c['surprise_pct']:+.1f}% "
            f"age={c['age_days']}d drift={c['drift_pct']:+.1f}% score={c['score']:.0f} | ${amount:,.0f}"
        )
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash")
                trade_logger.log_event(
                    "gate_failed",
                    "earnmom",
                    sym,
                    gate="free_cash",
                    amount=round(amount, 2),
                    reason="cannot free cash from SPY base",
                )
                continue
            trade_logger.log_event(
                "gate_passed", "earnmom", sym, gate="free_cash", amount=round(amount, 2)
            )
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event(
                    "gate_passed", "earnmom", sym, gate="circuit_breaker", amount=round(amount, 2)
                )
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event(
                    "gate_failed", "earnmom", sym, gate="emergency_liquidation", reason=str(emerg)
                )
                raise
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} circuit breaker: {halt}")
                trade_logger.log_event(
                    "gate_failed", "earnmom", sym, gate="circuit_breaker", reason=str(halt)
                )
                continue
            result = broker.buy(
                sym,
                dollar_amount=amount,
                stop_loss_pct=config.EARNMOM_STOP_PCT,
                take_profit_pct=config.EARNMOM_TARGET_PCT,
                strategy="earnmom",
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event(
                    "order_skipped", "earnmom", sym, gate="broker_buy", reason=result.get("reason")
                )
                continue
            if not result.get("stop_attached"):
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event(
                    "order_skipped",
                    "earnmom",
                    sym,
                    gate="stop_attach",
                    reason="stop-loss attach failed — flattened",
                    qty=result["qty"],
                    price=result["price"],
                )
                continue

            log.info(
                f"  ✓ EarnMom {sym} {result['qty']} sh @ ${result['price']:.2f} "
                f"SL={result['stop']} TP={result['target']} (hold {config.EARNMOM_HOLD_DAYS}d)"
            )
            trade_logger.log_event(
                "order_placed",
                "earnmom",
                sym,
                qty=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=result["target"],
                surprise_pct=c["surprise_pct"],
                amount=round(amount, 2),
                hold_days=config.EARNMOM_HOLD_DAYS,
            )

            pead_track(
                sym,
                result["price"],
                surprise_pct=c.get("surprise_pct", 0),
                report_date=c.get("report_date", datetime.date.today().isoformat()),
                strategy="earnmom",
                hold_days=config.EARNMOM_HOLD_DAYS,
            )
            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=result["target"],
                reason=(
                    f"EarnMom surprise={c['surprise_pct']:+.1f}%"
                    f" drift={c['drift_pct']:+.1f}% age={c['age_days']}d"
                ),
            )
            _mark_bought(sym, result)
            _append_trade_log(
                {
                    "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                    "symbol": sym,
                    "side": "buy",
                    "qty": result.get("qty"),
                    "price": result.get("price"),
                    "stop": result.get("stop"),
                    "target": None,
                    "strategy": "earnmom",
                    "surprise_pct": c["surprise_pct"],
                    "drift_pct": c["drift_pct"],
                    "age_days": c["age_days"],
                    "exit_date": None,
                    "exit_price": None,
                    "pnl_pct": None,
                }
            )
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
        log.info(
            f"GapFill BUY {sym} | gap={c['gap_pct']:+.2f}% "
            f"price=${c['price']:.2f} prior_close=${c['prior_close']:.2f}"
        )
        trade_logger.log_event(
            "signal_detected",
            "gapfill",
            sym,
            price=c["price"],
            gap_pct=c["gap_pct"],
            prior_close=c["prior_close"],
            target=c["target"],
        )
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
            sym,
            dollar_amount=amount,
            stop_loss_pct=config.GAPFILL_STOP_PCT,
            take_profit_pct=None,
            strategy="gapfill",
        )
        if result.get("blocked"):
            log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
            continue
        if not result.get("stop_attached"):
            broker.sell(sym, qty=result["qty"])
            log.info(f"  ✗ {sym} stop-attach failed — flattened {result['qty']} sh")
            continue

        log.info(
            f"  ✓ GapFill {sym} {result['qty']} sh @ ${result['price']:.2f} "
            f"SL=${result['stop']} target=${c['target']:.2f}"
        )
        trade_logger.log_event(
            "order_placed",
            "gapfill",
            sym,
            qty=result["qty"],
            price=result["price"],
            stop=result["stop"],
            target=c["target"],
            amount=round(amount, 2),
            gap_pct=c["gap_pct"],
        )
        _mark_bought(sym, result)
        pead_track(
            sym,
            result["price"],
            surprise_pct=c["gap_pct"],
            report_date=datetime.date.today().isoformat(),
            strategy="gapfill",
            hold_days=1,
        )
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
            trade_logger.log_event(
                "order_skipped",
                "momentum",
                sym,
                gate="news_filter",
                reason=_mnews.get("reason", ""),
            )
            continue

        fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, fkp, "momentum", log):
            continue

        amount = pv * 0.03
        log.info(
            f"Momentum BUY {sym} | {c['streak_days']}d streak "
            f"+{c['momentum_pct']}% RV={c['rel_volume']}x score={c['score']}"
        )
        trade_logger.log_event(
            "signal_detected",
            "momentum",
            sym,
            price=c["price"],
            streak_days=c["streak_days"],
            momentum_pct=c["momentum_pct"],
            rel_volume=c["rel_volume"],
            score=c["score"],
        )
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
            sym,
            dollar_amount=amount,
            stop_loss_pct=config.MOMENTUM_STOP_PCT,
            take_profit_pct=config.MOMENTUM_TAKE_PROFIT_PCT,
            strategy="momentum",
        )
        if result.get("blocked"):
            log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
            continue
        if not result.get("stop_attached"):
            broker.sell(sym, qty=result["qty"])
            log.info(f"  ✗ {sym} stop-attach failed — flattened {result['qty']} sh")
            continue

        log.info(
            f"  ✓ Momentum {sym} {result['qty']} sh @ ${result['price']:.2f} "
            f"SL=${result['stop']} TP=${result['target']}"
        )
        trade_logger.log_event(
            "order_placed",
            "momentum",
            sym,
            qty=result["qty"],
            price=result["price"],
            stop=result["stop"],
            target=result["target"],
            amount=round(amount, 2),
        )
        _mark_bought(sym, result)
        pead_track(
            sym,
            result["price"],
            surprise_pct=c["score"],
            report_date=datetime.date.today().isoformat(),
            strategy="momentum",
            hold_days=c["hold_days"],
        )
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
        log.info(
            f"Sector BUY {sym} [{c['sector']}] | "
            f"stock+{c['stock_ret']}% sector+{c['sector_ret']}% "
            f"RS={c['rs']} score={c['score']}"
        )
        trade_logger.log_event(
            "signal_detected",
            "sector",
            sym,
            price=c["price"],
            sector=c["sector"],
            sector_ret=c["sector_ret"],
            stock_ret=c["stock_ret"],
            rs=c["rs"],
            score=c["score"],
        )
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
            sym,
            dollar_amount=amount,
            stop_loss_pct=config.SECTOR_STOP_PCT,
            take_profit_pct=config.SECTOR_TAKE_PROFIT_PCT,
            strategy="sector",
        )
        if result.get("blocked"):
            log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
            continue
        if not result.get("stop_attached"):
            broker.sell(sym, qty=result["qty"])
            log.info(f"  ✗ {sym} stop-attach failed — flattened {result['qty']} sh")
            continue

        log.info(
            f"  ✓ Sector {sym} {result['qty']} sh @ ${result['price']:.2f} "
            f"SL=${result['stop']} TP=${result['target']} [{c['sector']}]"
        )
        trade_logger.log_event(
            "order_placed",
            "sector",
            sym,
            qty=result["qty"],
            price=result["price"],
            stop=result["stop"],
            target=result["target"],
            amount=round(amount, 2),
            sector=c["sector"],
        )
        _mark_bought(sym, result)
        pead_track(
            sym,
            result["price"],
            surprise_pct=c["score"],
            report_date=datetime.date.today().isoformat(),
            strategy="sector",
            hold_days=c["hold_days"],
        )
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
            log.info(
                f"VCP: watchlist stale ({wl.get('generated', '?')[:10]}) — running inline screen"
            )
    except FileNotFoundError:
        log.info("VCP: no pre_market_watchlist.json — running inline screen")
    except Exception as e:
        log.warning(f"VCP: watchlist load failed ({e}) — running inline screen")

    if watchlist is None:
        try:
            raw = screen()[:15]
            buy_list = [
                {
                    **s,
                    "score": s.get("raw_score", s.get("score", 0)),
                    "action": "BUY",
                    "reason": f"inline screen raw={s.get('raw_score', s.get('score', 0))}",
                }
                for s in sorted(
                    raw, key=lambda x: x.get("raw_score", x.get("score", 0)), reverse=True
                )
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
            "signal_detected",
            "vcp",
            sym,
            score=c.get("score"),
            reason=c.get("reason", ""),
        )

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            trade_logger.log_event(
                "order_skipped", "vcp", sym, gate="already_held", reason="already holding"
            )
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            trade_logger.log_event(
                "order_skipped", "vcp", sym, gate="idempotency", reason="already bought today"
            )
            continue

        _news = _today_brief.get("stock_news", {}).get(sym, {})
        if _news.get("skip"):
            log.info(f"  ✗ {sym} SKIP — news risk: {_news.get('reason', 'flagged by research')}")
            trade_logger.log_event(
                "order_skipped", "vcp", sym, gate="news_filter", reason=_news.get("reason", "")
            )
            continue

        _fkp = getattr(config, "FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")
        if not _sector_gate(sym, sector_counts, _fkp, "vcp", log):
            continue

        log.info(
            f"VCP BUY {sym} | score={c.get('score')} | {str(c.get('reason', ''))[:60]} | ${amount:,.0f}"
        )
        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"  ✗ {sym} SKIP — cannot free cash from SPY base")
                trade_logger.log_event(
                    "gate_failed",
                    "vcp",
                    sym,
                    gate="free_cash",
                    amount=round(amount, 2),
                    reason="cannot free cash from SPY base",
                )
                continue
            trade_logger.log_event(
                "gate_passed", "vcp", sym, gate="free_cash", amount=round(amount, 2)
            )
            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
                trade_logger.log_event(
                    "gate_passed", "vcp", sym, gate="circuit_breaker", amount=round(amount, 2)
                )
            except EmergencyLiquidation as emerg:
                log.error(f"✗ {sym} EMERGENCY LIQUIDATION: {emerg}")
                trade_logger.log_event(
                    "gate_failed", "vcp", sym, gate="emergency_liquidation", reason=str(emerg)
                )
                raise
            except TradingHalted as halt:
                log.warning(f"  ✗ {sym} blocked by circuit breaker: {halt}")
                trade_logger.log_event(
                    "gate_failed", "vcp", sym, gate="circuit_breaker", reason=str(halt)
                )
                continue
            result = broker.buy(
                sym,
                dollar_amount=amount,
                stop_loss_pct=config.VCP_STOP_PCT,
                take_profit_pct=None,
                strategy="vcp",
            )
            if result.get("blocked"):
                log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
                trade_logger.log_event(
                    "order_skipped", "vcp", sym, gate="broker_buy", reason=result.get("reason")
                )
                continue
            if not result.get("stop_attached"):
                log.error(f"  ✗ {sym} stop NOT attached — flattening")
                broker.sell(sym, qty=result["qty"])
                trade_logger.log_event(
                    "order_skipped",
                    "vcp",
                    sym,
                    gate="stop_attach",
                    reason="stop-loss attach failed — flattened",
                    qty=result["qty"],
                    price=result["price"],
                )
                continue

            log.info(
                f"  ✓ VCP {sym} {result['qty']} sh @ ${result['price']:.2f} "
                f"SL={result['stop']} (hold {config.VCP_HOLD_DAYS}d)"
            )
            trade_logger.log_event(
                "order_placed",
                "vcp",
                sym,
                qty=result["qty"],
                price=result["price"],
                stop=result["stop"],
                amount=round(amount, 2),
                hold_days=config.VCP_HOLD_DAYS,
            )

            pead_track(
                sym,
                result["price"],
                surprise_pct=c.get("score", 0),
                report_date=datetime.date.today().isoformat(),
                strategy="vcp",
                hold_days=config.VCP_HOLD_DAYS,
            )
            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=None,
                reason=f"VCP score={c.get('score')} {str(c.get('reason', ''))[:80]}",
            )
            _mark_bought(sym, result)
            _append_trade_log(
                {
                    "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                    "symbol": sym,
                    "side": "buy",
                    "qty": result.get("qty"),
                    "price": result.get("price"),
                    "stop": result.get("stop"),
                    "target": None,
                    "strategy": "vcp",
                    "score": c.get("score"),
                    "exit_date": None,
                    "exit_price": None,
                    "pnl_pct": None,
                }
            )
            slots[0] -= 1
            if slots[0] <= 0:
                log.info("Slots exhausted — VCP stopping")
                break
        except Exception as e:
            log.error(f"  ✗ VCP {sym} failed: {e}")


def _run_crypto(broker, cb, pv, slots, held, already_bought_today, sector_counts):
    """Crypto momentum: buy BTC/USD, ETH/USD, SOL/USD on 24h breakout."""
    import time as _time

    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    from core.crypto_screener import screen as crypto_screen

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

        log.info(
            f"Crypto BUY {sym} | momentum={c['momentum_pct']:+.1f}% | vol×{c['vol_ratio']:.1f} | ${amount:,.0f}"
        )
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

            spread_check = broker.check_crypto_spread(sym)
            if not spread_check.get("ok"):
                log.warning(
                    f"  ✗ {sym} SKIP — spread gate: {spread_check.get('reason')} "
                    f"(spread={spread_check.get('spread_pct')})"
                )
                trade_logger.log_event(
                    "order_skipped",
                    "crypto",
                    sym,
                    gate="spread_check",
                    reason=spread_check.get("reason"),
                    spread_pct=spread_check.get("spread_pct"),
                )
                continue

            if config.DRY_RUN:
                log.info(
                    f"[DRY_RUN] Would BUY {sym} ${notional:.2f} notional @ ~${c['price']:,.2f} "
                    "-- no order submitted"
                )
                trade_logger.log_event(
                    "dry_run_order",
                    "crypto",
                    sym,
                    side="buy",
                    notional=notional,
                    ref_price=c["price"],
                    spread_pct=spread_check.get("spread_pct"),
                )
                cost_tracker.record_fill(
                    strategy="crypto",
                    symbol=sym,
                    side="buy",
                    signal_price=c["price"],
                    fill_price=c["price"],
                    qty=round(notional / c["price"], 9),
                    spread_pct=spread_check.get("spread_pct"),
                )
                continue

            order = broker.trade.submit_order(
                MarketOrderRequest(
                    symbol=sym,
                    notional=notional,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.GTC,
                )
            )
            log.info(f"Crypto BUY {sym} ${notional:.2f} notional submitted [{str(order.id)[:8]}]")

            fill_price = None
            filled_qty = 0.0
            for _ in range(10):
                try:
                    o = broker.trade.get_order_by_id(order.id)
                    if o.filled_avg_price:
                        fill_price = float(o.filled_avg_price)
                        filled_qty = (
                            float(o.filled_qty) if o.filled_qty else round(notional / fill_price, 9)
                        )
                        break
                except Exception:
                    pass
                _time.sleep(0.5)

            basis = fill_price or c["price"]
            if filled_qty <= 0:
                filled_qty = round(notional / basis, 9)

            if fill_price is not None:
                cost_tracker.record_fill(
                    strategy="crypto",
                    symbol=sym,
                    side="buy",
                    signal_price=c["price"],
                    fill_price=fill_price,
                    qty=filled_qty,
                    spread_pct=spread_check.get("spread_pct"),
                )

            stop = round(basis * (1 - config.VCP_STOP_PCT), 2)
            stop_attached, _ = broker.attach_stop_target(sym, filled_qty, stop, None)

            log.info(
                f"  ✓ Crypto {sym} {filled_qty:.6f} @ ${basis:,.2f} SL=${stop:,.2f} stop_attached={stop_attached}"
            )
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
            _append_trade_log(
                {
                    "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
                    "symbol": sym,
                    "side": "buy",
                    "qty": filled_qty,
                    "price": basis,
                    "stop": stop,
                    "target": None,
                    "strategy": "crypto",
                    "score": c.get("score"),
                    "exit_date": None,
                    "exit_price": None,
                    "pnl_pct": None,
                }
            )
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
    "pead": _run_pead,
    "meanrev": _run_meanrev,
    "insider": _run_insider,
    "squeeze": _run_squeeze,
    "breakout": _run_breakout,
    "earnmom": _run_earnmom,
    "gapfill": _run_gapfill,
    "momentum": _run_momentum,
    "sector": _run_sector,
    "vcp": _run_vcp,
    "crypto": _run_crypto,
    "macross": _run_macross,
    "buffett_value": _run_buffett_value,
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
        log.info(
            f"Reconciled {reconciled} closed trades from Alpaca order history into trade_log.jsonl"
        )

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
    slots = [
        min(MAX_BUYS, config.MAX_OPEN_POSITIONS - pos_count)
    ]  # mutable: handlers decrement in-place

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

    # ── Re-attach protection missing a live stop ─────────────────────────────
    # A fractional (DAY-tif) protective exit expires at the prior session's
    # close and can't survive overnight, so any fractional position held
    # across a night needs a fresh stop before we consider opening new ones.
    # Whole-share GTC stops are unaffected (already live, skipped as-is).
    try:
        flattened = reattach_missing_protection(broker, config, log)
        if flattened:
            held -= flattened
    except Exception as e:
        log.warning(f"Protection re-attach pass failed (non-blocking): {e}")

    already_bought_today = _load_today_bought()
    if already_bought_today:
        log.info(f"Already bought today (idempotency): {sorted(already_bought_today)}")

    # ── Load today's research brief (built by pre_market at 6 AM) ────────────
    global _today_brief
    try:
        from core.researcher import load_today_brief

        _today_brief = load_today_brief()
        if _today_brief:
            log.info(
                "Research brief: risk=%s | %s",
                _today_brief.get("macro_risk", "?"),
                _today_brief.get("summary", "")[:80],
            )
            if _today_brief.get("trade_bias_override") == "cash":
                _evt = (_today_brief.get("event_blocks") or [{}])[0]
                log.warning(
                    "RESEARCH OVERRIDE: CASH — %s", _evt.get("event", "high-impact event today")
                )
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
        highs = [b["high"] for b in spy_bars]
        lows = [b["low"] for b in spy_bars]
        closes = [b["close"] for b in spy_bars]
        volumes = [b.get("volume") for b in spy_bars]
        if any(v is None for v in volumes):
            volumes = (
                None  # missing volume on any bar -> HMM gate falls back to its 2-feature model
            )

        # REGIME_GATE_MODE=hmm opts into the HMM-based gate (regime_gate_hmm.py);
        # default "sma_adx" (or unset) keeps today's proven ADX/SMA gate as-is.
        gate_mode = os.environ.get("REGIME_GATE_MODE", "sma_adx").strip().lower()
        hmm_reg = None
        if gate_mode == "hmm":
            from regime_gate_hmm import classify as classify_hmm

            reg = classify_hmm(highs, lows, closes, volumes=volumes)
            hmm_reg = reg
        else:
            reg = classify(highs, lows, closes)
        log.info(
            f"Regime gate ({gate_mode}): state={reg.state} trend={reg.trend} adx={reg.adx:.1f} sma50={reg.sma50:.2f} sma200={reg.sma200:.2f} reason={reg.reason}"
        )

        # Side-by-side shadow read: always log what the HMM gate would say,
        # without ever letting it gate unless REGIME_GATE_MODE=hmm above -
        # lets it be observed for real before ever being trusted to decide.
        if gate_mode != "hmm":
            try:
                from regime_gate_hmm import classify as classify_hmm_shadow

                hmm_reg = classify_hmm_shadow(highs, lows, closes, volumes=volumes)
                log.info(
                    f"Regime gate (hmm, SHADOW - not gating): state={hmm_reg.state} "
                    f"confidence={hmm_reg.confidence:.0%} reason={hmm_reg.reason}"
                )
            except Exception as e:
                log.warning(f"HMM shadow regime gate failed (non-blocking): {e}")

        # Advisory-only: what a regime-based exposure scaler would suggest, purely
        # for observation - never applied to position sizing or the gate decision.
        if hmm_reg is not None:
            try:
                from regime_exposure import suggest_exposure

                suggestion = suggest_exposure(hmm_reg)
                log.info(
                    f"Regime exposure suggestion (ADVISORY, not applied): bucket={suggestion.bucket} "
                    f"target_exposure={suggestion.target_exposure_pct:.0%} max_leverage={suggestion.max_leverage}x "
                    f"trailing_stop={suggestion.trailing_stop_pct} reason={suggestion.reason}"
                )
            except Exception as e:
                log.warning(f"Regime exposure suggestion failed (non-blocking): {e}")

        if not reg.can_trade:
            log.warning(f"REGIME GATE: STAND_DOWN — {reg.reason} — holding cash, no screening")
            return
    else:
        log.warning("Regime gate SKIPPED: no SPY bars available.")
        # Proceed with NEUTRAL regime so strategy still runs, but log explicitly
        log.info("Regime: fallback NEUTRAL (SPY bars unavailable)")

    # Emergency liquidation check before strategy loop
    if cb.liquidation_required():
        log.error(
            f"EMERGENCY LIQUIDATION: equity ${equity_now:,.2f} vs day-start ${day_start:,.2f} ({day_pnl:+.2f}%)"
        )
        try:
            broker.cancel_all_orders()
            positions = broker.get_positions()
            for p in positions:
                if is_base_symbol(p.symbol):
                    continue
                try:
                    broker.close_position(p.symbol)
                    log.info(f"  Emergency closed {p.symbol}")
                    buffett_untrack(p.symbol)  # no-op if not a Buffett Value position
                except Exception as e:
                    log.warning(f"  Emergency close {p.symbol} failed: {e}")
        except Exception as e:
            log.error(f"Emergency liquidation attempt failed: {e}")
        send_trade_alert(
            action="EMERGENCY",
            ticker="ALL",
            shares=0,
            price=0,
            stop=0,
            target=0,
            reason="Emergency liquidation: emergency threshold breached",
        )
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
            log.info("No slots remaining — stopping strategy loop")
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
