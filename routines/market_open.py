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

from circuit_breaker import CircuitBreaker, TradingHalted
from core import composite, config, logger
from core.broker import BrokerClient
from core.earnings_screener import screen_earnings
from core.edge import circuit_breaker_tripped
from core.notifier import send_trade_alert
from core.pead_tracker import add_position as pead_track
from core.screener import screen
from core.spy_base import free_cash_for_pead, rebalance_to_spy
from core.spy_base import log_status as spy_log
from core.universe import build_universe
from kelly_sizing import KellySizer, stats_from_trades
from regime_gate import classify

log = logger.setup("market_open")
ET  = pytz.timezone("America/New_York")


def _build_breaker(broker: BrokerClient) -> CircuitBreaker:
    return CircuitBreaker(
        get_account=broker.get_account,
        get_positions=broker.get_positions,
        max_open_positions=3,
        max_position_pct=0.05,
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


# ── Shared buy helper ─────────────────────────────────────────────────────────

def _execute_buy(
    broker,
    cb,
    pv: float,
    sym: str,
    size_pct: float,
    strategy: str,
    reason: str,
    already_bought_today: set,
    extra_log: dict | None = None,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> bool:
    """Submit one buy order with circuit-breaker check, log, and alert.

    Returns True if a share was bought (caller should count toward buys_taken).
    Mutates *already_bought_today* in-place on success.
    """
    amount = pv * size_pct
    kwargs: dict = {"dollar_amount": amount}
    if stop_loss_pct is not None:
        kwargs["stop_loss_pct"] = stop_loss_pct
    if take_profit_pct is not None:
        kwargs["take_profit_pct"] = take_profit_pct

    try:
        cb.check_before_order(intended_notional=amount, symbol=sym)
    except TradingHalted as halt:
        log.warning(f"  ✗ {sym} blocked by circuit breaker: {halt}")
        return False

    result = broker.buy(sym, **kwargs)
    if result.get("blocked"):
        log.warning(f"  ✗ {sym} buy blocked: {result.get('reason')}")
        return False
    if not result.get("stop_attached"):
        log.error(f"  ✗ {sym} bought but stop NOT attached — flattening")
        broker.sell(sym, qty=result["qty"])
        send_trade_alert(
            action="FLATTEN", ticker=sym, shares=result["qty"],
            price=result["price"], stop=None, target=None,
            reason=f"{strategy} stop-loss attach failed — position rejected",
        )
        return False

    extra = extra_log or {}
    log.info(
        f"  ✓ {strategy.upper()} {sym} {result['qty']} sh @ ${result['price']:.2f} "
        f"SL={result['stop']} | {reason}"
    )
    send_trade_alert(
        action="BUY", ticker=sym, shares=result["qty"],
        price=result["price"], stop=result["stop"],
        target=result.get("target"),
        reason=f"[{strategy.upper()}] {reason}",
    )
    _mark_bought(sym, result)
    already_bought_today.add(sym)
    _append_trade_log({
        "ts":         datetime.datetime.now(ET).isoformat(timespec="seconds"),
        "symbol":     sym,
        "side":       "buy",
        "strategy":   strategy,
        "qty":        result.get("qty"),
        "price":      result.get("price"),
        "stop":       result.get("stop"),
        "target":     result.get("target"),
        "exit_date":  None,
        "exit_price": None,
        "pnl_pct":    None,
        **extra,
    })
    return True


# ── Strategy runners ──────────────────────────────────────────────────────────

def _run_pead(broker, cb, pv, slots, held, already_bought_today) -> int:
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
        return 0

    for c in candidates:
        log.info(f"  • {c['symbol']} surprise={c['surprise_pct']:+.1f}% "
                 f"EPS={c.get('actual_eps')}/{c.get('estimated_eps')} "
                 f"reported={c['report_date']} price=${c.get('price', 0):.2f}")

    buys_taken = 0
    for c in candidates[:slots]:
        sym      = c["symbol"]
        surprise = c["surprise_pct"]

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            continue
        if sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — already bought today")
            continue

        amount = pv * config.PEAD_SIZE_PCT
        log.info(f"PEAD BUY {sym} | surprise={surprise:+.1f}% | "
                 f"size={config.PEAD_SIZE_PCT*100:.0f}% | ${amount:,.0f}")

        try:
            if not free_cash_for_pead(broker, amount):
                log.warning(f"✗ {sym} SKIP — cannot free ${amount:,.0f} from SPY base")
                continue

            try:
                cb.check_before_order(intended_notional=amount, symbol=sym)
            except TradingHalted as halt:
                log.warning(f"✗ {sym} blocked by circuit breaker: {halt}")
                continue

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
                    price=result["price"], stop=None, target=None,
                    reason="PEAD stop-loss attach failed — position rejected",
                )
                continue

            log.info(f"✓ PEAD {sym} {result['qty']} sh @ ${result['price']:.2f} "
                     f"SL={result['stop']} (hold {config.PEAD_HOLD_DAYS}d)")
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
            already_bought_today.add(sym)
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

    spy_log(broker)
    spy_result = rebalance_to_spy(broker)
    if spy_result["action"] not in ("none", "disabled"):
        log.info(f"SPY base: {spy_result['action']} {spy_result.get('qty', 0)} shares")

    return buys_taken


def _run_vcp(broker, cb, pv, slots, held, already_bought_today) -> int:
    """VCP momentum strategy: breakout from volatility-contraction base."""
    universe = build_universe()
    log.info(f"VCP: Universe {len(universe)} symbols → screening...")
    candidates = screen(universe)
    log.info(f"VCP: {len(candidates)} raw candidates")

    if not candidates:
        log.info("VCP: no candidates — done")
        return 0

    passed = []
    for c in candidates:
        sym   = c["symbol"]
        price = c.get("price", 0)
        rs    = c.get("rs_vs_spy")
        gap   = c.get("gap_pct", 0)
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

        rs_str = f"RS{rs:+.1f}%" if rs is not None else "RS n/a"
        log.info(f"  ✓ {sym} PASS — ${price:.2f} score={score} {rs_str} gap={gap:+.1f}%")
        passed.append(c)

    log.info(f"VCP passed skills: {len(passed)}/{len(candidates)}")
    if not passed:
        return 0

    log.info("VCP: composite scoring...")
    passed_syms = [c["symbol"] for c in passed]
    ctx = composite.build_context(extra_symbols=passed_syms)
    log.info(f"Market regime score={ctx['regime_score']} mult={ctx['regime_mult']}")

    bars_map = {}
    try:
        from core.screener import _fetch_bars
        bars_map = _fetch_bars(passed_syms, days=60)
    except Exception as e:
        log.warning(f"VCP composite bar fetch failed (non-blocking): {e}")

    scored = []
    for c in passed:
        result = composite.compute_composite(c, bars_map.get(c["symbol"]), ctx)
        c["composite"]       = result["composite"]
        c["composite_final"] = result["final"]
        c["composite_breakdown"] = result["breakdown"]
        log.info(composite.format_breakdown(result))
        scored.append(c)

    scored.sort(key=lambda x: x.get("composite_final", 0), reverse=True)
    ranked = " > ".join(
        f"{c['symbol']}({c.get('composite_final', 0):.1f})" for c in scored
    )
    log.info(f"VCP composite ranking: {ranked}")

    buys_taken = 0
    for c in scored[:slots]:
        sym    = c["symbol"]
        score  = c.get("score", 0)
        rs     = c.get("rs_vs_spy")
        comp   = c.get("composite_final", 0)
        amount = pv * config.MAX_POSITION_SIZE_PCT

        kelly_frac, kelly_reason = _compute_kelly_shadow(pv)
        log.info(
            f"KELLY_SHADOW {sym} | would_size={kelly_frac:.2%} | "
            f"flat_size={config.MAX_POSITION_SIZE_PCT:.2%} | "
            f"diff={kelly_frac - config.MAX_POSITION_SIZE_PCT:+.2%} | {kelly_reason}"
        )
        log.info(f"VCP BUY {sym} | composite={comp:.1f} (vcp={score}) | "
                 f"size={config.MAX_POSITION_SIZE_PCT*100:.0f}% | ${amount:,.0f}")
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
                    action="FLATTEN", ticker=sym, shares=result["qty"],
                    price=result["price"], stop=None, target=None,
                    reason="stop-loss attach failed at entry — unprotected position rejected",
                )
                continue
            log.info(f"✓ VCP {sym} {result['qty']} sh @ ${result['price']:.2f} "
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
            already_bought_today.add(sym)
            _append_trade_log({
                "ts":         datetime.datetime.now(ET).isoformat(timespec="seconds"),
                "symbol":     sym,
                "side":       "buy",
                "strategy":   "vcp",
                "qty":        result.get("qty"),
                "price":      result.get("price"),
                "stop":       result.get("stop"),
                "target":     result.get("target"),
                "exit_date":  None,
                "exit_price": None,
                "pnl_pct":    None,
            })
            buys_taken += 1
        except Exception as e:
            log.error(f"✗ VCP {sym} buy failed: {e}")

    log.info(f"VCP complete | Buys taken: {buys_taken}")
    return buys_taken


def _run_meanrev(broker, cb, pv, slots, held, already_bought_today) -> int:
    """Mean-reversion: RSI<30 + Bollinger Band oversold, price above SMA200."""
    from core.meanrev_screener import screen as mr_screen

    log.info("MeanRev: scanning 80-stock S&P universe...")
    candidates = mr_screen(
        rsi_threshold=config.MEANREV_RSI_THRESHOLD,
        bb_std=config.MEANREV_BB_STD,
        min_price=config.MEANREV_MIN_PRICE,
        min_avg_volume=config.MEANREV_MIN_AVG_VOL,
    )
    log.info(f"MeanRev: {len(candidates)} candidates")

    buys_taken = 0
    for c in candidates[:slots]:
        sym = c["symbol"]
        if sym in held or sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — held or bought today")
            continue
        reason = (f"RSI={c['rsi']:.1f} BB_lower={c['bb_lower']:.2f} "
                  f"SMA200={c['sma200']:.2f} score={c['score']}")
        bought = _execute_buy(
            broker, cb, pv, sym, config.MEANREV_SIZE_PCT,
            "meanrev", reason, already_bought_today,
            extra_log={"rsi": c["rsi"], "sma200": c["sma200"], "score": c["score"]},
        )
        if bought:
            buys_taken += 1

    log.info(f"MeanRev complete | Buys taken: {buys_taken}")
    return buys_taken


def _run_insider(broker, cb, pv, slots, held, already_bought_today) -> int:
    """Insider cluster: CEO/CFO purchase clusters scored by seniority + size."""
    from core.insider_screener import screen as ins_screen

    log.info("Insider: scanning for P-Purchase cluster setups...")
    candidates = ins_screen(
        lookback_days=config.INSIDER_LOOKBACK_DAYS,
        min_score=config.INSIDER_MIN_SCORE,
        min_cluster=config.INSIDER_MIN_CLUSTER,
    )
    log.info(f"Insider: {len(candidates)} candidates")

    buys_taken = 0
    for c in candidates[:slots]:
        sym = c["symbol"]
        if sym in held or sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — held or bought today")
            continue
        reason = (f"cluster={c['cluster_count']} seniority={c['max_seniority']} "
                  f"total_$={c['total_dollars']:,} score={c['score']}")
        bought = _execute_buy(
            broker, cb, pv, sym, config.INSIDER_SIZE_PCT,
            "insider", reason, already_bought_today,
            extra_log={"cluster": c["cluster_count"], "insider_score": c["score"]},
        )
        if bought:
            buys_taken += 1

    log.info(f"Insider complete | Buys taken: {buys_taken}")
    return buys_taken


def _run_squeeze(broker, cb, pv, slots, held, already_bought_today) -> int:
    """Short-squeeze: SI>15% + DTC>3 + positive momentum."""
    from core.squeeze_screener import screen as sq_screen

    log.info("Squeeze: scanning for short-squeeze setups...")
    candidates = sq_screen(
        si_min_pct=config.SQUEEZE_SI_MIN_PCT,
        dtc_min=config.SQUEEZE_DTC_MIN,
        min_price=config.SQUEEZE_MIN_PRICE,
        min_avg_volume=config.SQUEEZE_MIN_AVG_VOL,
    )
    log.info(f"Squeeze: {len(candidates)} candidates")

    buys_taken = 0
    for c in candidates[:slots]:
        sym = c["symbol"]
        if sym in held or sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — held or bought today")
            continue
        reason = (f"SI={c['si_pct']:.1f}% DTC={c['dtc']:.1f} "
                  f"mom1m={c['momentum_1m_pct']:+.1f}% score={c['score']}")
        bought = _execute_buy(
            broker, cb, pv, sym, config.SQUEEZE_SIZE_PCT,
            "squeeze", reason, already_bought_today,
            extra_log={"si_pct": c["si_pct"], "dtc": c["dtc"]},
        )
        if bought:
            buys_taken += 1

    log.info(f"Squeeze complete | Buys taken: {buys_taken}")
    return buys_taken


def _run_breakout(broker, cb, pv, slots, held, already_bought_today) -> int:
    """50-day resistance breakout with 1.5× volume confirmation."""
    from core.breakout_screener import screen as bo_screen

    log.info("Breakout: scanning for 50-day resistance breakouts...")
    candidates = bo_screen(
        vol_mult_min=config.BREAKOUT_VOL_MULT_MIN,
        min_price=config.BREAKOUT_MIN_PRICE,
        min_avg_volume=config.BREAKOUT_MIN_AVG_VOL,
    )
    log.info(f"Breakout: {len(candidates)} candidates")

    buys_taken = 0
    for c in candidates[:slots]:
        sym = c["symbol"]
        if sym in held or sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — held or bought today")
            continue
        reason = (f"price={c['price']:.2f} vs resistance={c['resistance_50']:.2f} "
                  f"(+{c['pct_above_resistance']:.1f}%) vol×{c['vol_mult']:.1f} score={c['score']}")
        bought = _execute_buy(
            broker, cb, pv, sym, config.BREAKOUT_SIZE_PCT,
            "breakout", reason, already_bought_today,
            extra_log={"pct_above_resistance": c["pct_above_resistance"],
                       "vol_mult": c["vol_mult"]},
        )
        if bought:
            buys_taken += 1

    log.info(f"Breakout complete | Buys taken: {buys_taken}")
    return buys_taken


def _run_earnmom(broker, cb, pv, slots, held, already_bought_today) -> int:
    """Earnings-momentum drift: beat EPS 8–45 days ago, still drifting up."""
    from core.earnings_momentum_screener import screen as em_screen

    log.info("EarnMom: scanning for post-earnings drift continuation...")
    candidates = em_screen(
        lookback_min=config.EARNMOM_LOOKBACK_MIN,
        lookback_max=config.EARNMOM_LOOKBACK_MAX,
        min_surprise_pct=config.EARNMOM_MIN_SURPRISE_PCT,
        min_drift_pct=config.EARNMOM_MIN_DRIFT_PCT,
        min_price=config.EARNMOM_MIN_PRICE,
        min_avg_volume=config.EARNMOM_MIN_AVG_VOL,
    )
    log.info(f"EarnMom: {len(candidates)} candidates")

    buys_taken = 0
    for c in candidates[:slots]:
        sym = c["symbol"]
        if sym in held or sym in already_bought_today:
            log.info(f"  ✗ {sym} SKIP — held or bought today")
            continue
        reason = (f"surprise={c['surprise_pct']:+.1f}% drift={c['drift_pct']:+.1f}% "
                  f"since={c['report_date']} score={c['score']}")
        bought = _execute_buy(
            broker, cb, pv, sym, config.EARNMOM_SIZE_PCT,
            "earnmom", reason, already_bought_today,
            extra_log={"surprise_pct": c["surprise_pct"], "drift_pct": c["drift_pct"],
                       "report_date": c["report_date"]},
        )
        if bought:
            buys_taken += 1

    log.info(f"EarnMom complete | Buys taken: {buys_taken}")
    return buys_taken


# ── Strategy router ────────────────────────────────────────────────────────────

_STRATEGY_RUNNERS = {
    "pead":     _run_pead,
    "vcp":      _run_vcp,
    "meanrev":  _run_meanrev,
    "insider":  _run_insider,
    "squeeze":  _run_squeeze,
    "breakout": _run_breakout,
    "earnmom":  _run_earnmom,
}


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

    # ── Regime gate (shared by all strategies) ───────────────────────────────
    try:
        from core.screener import _fetch_bars
        spy_bars = (_fetch_bars(["SPY"], days=400) or {}).get("SPY") or []
    except Exception as e:
        log.warning(f"Regime gate SPY bars fetch failed (non-blocking): {e}")
        spy_bars = []
    if spy_bars:
        highs  = [b["high"]  for b in spy_bars]
        lows   = [b["low"]   for b in spy_bars]
        closes = [b["close"] for b in spy_bars]
        reg = classify(highs, lows, closes)
        log.info(f"Regime gate: state={reg.state} trend={reg.trend} adx={reg.adx:.1f} "
                 f"sma50={reg.sma50:.2f} sma200={reg.sma200:.2f} reason={reg.reason}")
        if not reg.can_trade:
            log.warning(f"REGIME GATE: STAND_DOWN — {reg.reason} — holding cash, no screening")
            return
    else:
        log.warning("Regime gate skipped: no SPY bars available — proceeding without gate")

    # ── Strategy router ───────────────────────────────────────────────────────
    strategies = config.STRATEGY_MODES
    log.info(f"Strategy mode(s): {', '.join(s.upper() for s in strategies)}")

    total_buys = 0
    for strat in strategies:
        runner = _STRATEGY_RUNNERS.get(strat)
        if runner is None:
            log.warning(f"Unknown strategy '{strat}' in STRATEGY_MODE — skipping")
            continue
        remaining = slots - total_buys
        if remaining <= 0:
            log.info("All slots filled — skipping remaining strategies")
            break
        log.info(f"── {strat.upper()} ({remaining} slot(s) remaining) ──")
        bought = runner(broker, cb, pv, remaining, held, already_bought_today)
        total_buys += bought

    log.info(f"Market open complete | Total buys: {total_buys}")
    logger.banner(log, "MARKET OPEN COMPLETE")


if __name__ == "__main__":
    run()
