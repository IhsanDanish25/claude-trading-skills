from __future__ import annotations
"""
MARKET CLOSE ROUTINE — 3:00 PM ET, Mon-Fri
───────────────────────────────────────────
1. Final position check — exit anything weak before close
2. Cancel all open orders (no overnight limit orders)
3. Log day's P&L
4. Save daily trade log to /tmp/daily_log.json
5. FTD detection on SPY (market health signal)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import datetime
import pytz

from core import logger, config
from core.broker   import BrokerClient
from core.fmp      import get_quotes, get_market_breadth, get_daily_bars
from core.analyst  import review_open_positions, analyze_market_regime, detect_ftd
from core.notifier import send_eod_summary, send_trade_alert
from core.pead_tracker import get_expired, remove_position as pead_untrack, get_all as pead_all
from core.spy_base import log_status as spy_log

log = logger.setup("market_close")
ET  = pytz.timezone("America/New_York")

CLOSE_EXIT_THRESHOLD = -0.03   # Force exit if P&L < -3%
MAX_CASH_CLOSES_PER_DAY = 3    # Cap how many positions a cash-regime call can liquidate
DAILY_CLOSE_LOG = os.path.join(config.STATE_DIR, "daily_close_log.json")


def _today_close_count() -> int:
    today = datetime.date.today().isoformat()
    try:
        with open(DAILY_CLOSE_LOG) as f:
            data = json.load(f)
        if data.get("date") == today:
            return int(data.get("count", 0))
    except (FileNotFoundError, ValueError, KeyError):
        pass
    return 0


def _bump_close_count(n: int = 1) -> None:
    today = datetime.date.today().isoformat()
    cur = _today_close_count()
    try:
        with open(DAILY_CLOSE_LOG, "w") as f:
            json.dump({"date": today, "count": cur + n}, f)
    except Exception as e:
        log.warning(f"Could not persist close count: {e}")


def run():
    config.validate()
    logger.banner(log, "MARKET CLOSE — 3:00 PM ET")

    broker = BrokerClient()
    today  = datetime.date.today().isoformat()

    # ── Cancel all open day orders ────────────────────────────────────────────
    log.info("── Cancelling open orders")
    try:
        broker.cancel_all_orders()
    except Exception as e:
        log.warning(f"Cancel orders: {e}")

    # ── PEAD time-exit: close positions past hold period ───────────────────
    pead_positions = pead_all()
    if pead_positions:
        log.info(f"── PEAD positions tracked: {len(pead_positions)}")
        expired = get_expired()
        if expired:
            log.info(f"── PEAD time-exits due: {len(expired)}")
            for exp in expired:
                sym = exp["symbol"]
                age = exp["age_days"]
                hold = exp["hold_days"]
                log.info(f"  PEAD TIME-EXIT {sym} — held {age}d (limit {hold}d)")
                try:
                    broker.close_position(sym)
                    pead_untrack(sym)
                    send_trade_alert(
                        action="SELL",
                        ticker=sym,
                        shares=0,
                        price=0,
                        reason=f"PEAD time-exit: {age}d held (max {hold}d)",
                    )
                    log.info(f"  ✓ PEAD closed {sym} after {age} days")
                except Exception as e:
                    log.error(f"  ✗ PEAD close {sym} failed: {e}")
        else:
            for sym, info in pead_positions.items():
                from core.pead_tracker import position_age
                age = position_age(sym)
                log.info(f"  PEAD {sym}: day {age}/{info.get('hold_days', 60)} "
                         f"(surprise={info.get('surprise_pct', '?')}%)")

    # ── Position final review ─────────────────────────────────────────────────
    positions = broker.get_positions()
    log.info(f"── Positions at close: {len(positions)}")

    force_close = []   # symbols force-closed below -3%; referenced in EOD summary

    breadth = get_market_breadth()
    regime = analyze_market_regime(breadth)
    log.info(f"EOD regime: {regime['regime'].upper()} | Bias: {regime['trade_bias']}")

    if not positions:
        log.info("No positions to review")
    else:
        symbols = [p.symbol for p in positions]
        quotes  = get_quotes(symbols)

        pos_data = []

        for p in positions:
            sym      = p.symbol
            entry    = float(p.avg_entry_price)
            current  = float(quotes.get(sym, {}).get("price", entry))
            qty      = int(float(p.qty))
            pnl_pct  = (current - entry) / entry
            unrealized = float(p.unrealized_pl or 0)

            log.info(f"  {sym:6} | ${entry:.2f} → ${current:.2f} | "
                     f"{pnl_pct*100:+.2f}% | ${unrealized:+,.0f}")

            # Force-close deep losers before market shuts
            if pnl_pct <= CLOSE_EXIT_THRESHOLD:
                log.warning(f"  ⚠️  {sym} below -3% threshold — force close")
                force_close.append(sym)
            else:
                pos_data.append({
                    "symbol":        sym,
                    "entry_price":   entry,
                    "current_price": current,
                    "qty":           qty,
                    "pnl_pct":       pnl_pct * 100,
                    "unrealized_usd": unrealized,
                    "days_held":     1,
                    "stop":          round(entry * (1 - config.STOP_LOSS_PCT), 2),
                    "target":        round(entry * (1 + config.TAKE_PROFIT_PCT), 2),
                })

        # Execute force closes
        for sym in force_close:
            try:
                broker.close_position(sym)
                log.info(f"  ✓ Force-closed {sym}")
                _bump_close_count()
                q = quotes.get(sym, {})
                price = float(q.get("price", 0))
                send_trade_alert("SELL", sym, 0, price, 0, 0, reason="Force-closed: -3% threshold")
            except Exception as e:
                log.error(f"  ✗ Close {sym} failed: {e}")

        # Claude review on remaining
        if pos_data and regime["trade_bias"] == "cash":
            already_today = _today_close_count()
            remaining_budget = max(0, MAX_CASH_CLOSES_PER_DAY - already_today)
            if remaining_budget == 0:
                log.warning(
                    f"Cash regime — but daily close cap "
                    f"({MAX_CASH_CLOSES_PER_DAY}) already reached. "
                    f"Skipping remaining {len(pos_data)} closes to preserve capital."
                )
            else:
                log.warning(
                    f"Cash regime — closing up to {remaining_budget} positions "
                    f"(daily cap {MAX_CASH_CLOSES_PER_DAY}, used {already_today})"
                )
                closed_this_run = 0
                for pd in pos_data:
                    if closed_this_run >= remaining_budget:
                        log.warning(
                            f"  Hit daily cap — leaving {len(pos_data) - closed_this_run} "
                            f"positions for tomorrow's market_open to evaluate"
                        )
                        break
                    sym = pd["symbol"]
                    try:
                        broker.close_position(sym)
                        log.info(f"  ✓ Cash-regime close: {sym}")
                        _bump_close_count()
                        closed_this_run += 1
                    except Exception as e:
                        log.error(f"  ✗ {sym} close failed: {e}")

        elif pos_data:
            log.info(f"── Claude: EOD position review ({len(pos_data)} positions)")
            decisions = review_open_positions(pos_data, regime["regime"])
            for d in decisions:
                sym    = d.get("symbol", "")
                action = d.get("action", "HOLD")
                reason = d.get("reason", "")
                log.info(f"  {sym:6} → {action} | {reason}")

                if action == "SELL":
                    try:
                        broker.sell(sym)
                        log.info(f"  ✓ EOD sold {sym}")
                    except Exception as e:
                        log.error(f"  ✗ {e}")

    # ── FTD detection on SPY ──────────────────────────────────────────────────
    log.info("── FTD detection (SPY)")
    try:
        spy_bars = get_daily_bars("SPY", days=20)
        ftd_result = detect_ftd(spy_bars[:20])
        log.info(f"  FTD detected: {ftd_result['ftd_detected']}")
        log.info(f"  Confidence: {ftd_result['confidence']}")
        log.info(f"  Details: {ftd_result['details']}")
        if ftd_result.get("ftd_date"):
            log.info(f"  FTD date: {ftd_result['ftd_date']}")
    except Exception as e:
        log.error(f"FTD detection fail: {e}")
        ftd_result = {}

    # ── Day P&L summary ───────────────────────────────────────────────────────
    log.info("── End of day summary")
    acct = broker.get_account()
    pv   = float(acct.portfolio_value)
    cash = float(acct.cash)

    # Remaining positions
    final_positions = broker.get_positions()
    total_unrealized = sum(float(p.unrealized_pl or 0) for p in final_positions)

    log.info(f"  Portfolio value:   ${pv:,.2f}")
    log.info(f"  Cash:              ${cash:,.2f}")
    log.info(f"  Positions held:    {len(final_positions)}")
    log.info(f"  Unrealized P&L:    ${total_unrealized:+,.2f}")

    # Save daily log
    daily_log = {
        "date":             today,
        "portfolio_value":  pv,
        "cash":             cash,
        "positions_held":   len(final_positions),
        "unrealized_pnl":   total_unrealized,
        "regime":           regime.get("regime", "unknown"),
        "trade_bias":       regime.get("trade_bias", "unknown"),
        "ftd":              ftd_result,
        "spy_change_pct":   breadth.get("spy_change_pct", 0),
    }

    log_path = os.path.join(config.STATE_DIR, f"daily_log_{today}.json")
    with open(log_path, "w") as f:
        json.dump(daily_log, f, indent=2)
    log.info(f"  Daily log saved → {log_path}")

    send_eod_summary(
        date=today,
        portfolio_value=pv,
        cash=cash,
        positions_held=len(final_positions),
        unrealized_pnl=total_unrealized,
        regime=daily_log["regime"],
        bias=daily_log["trade_bias"],
        spy_change_pct=daily_log["spy_change_pct"],
        ftd_detected=ftd_result.get("ftd_detected", False),
        force_closed=force_close,
    )

    # ── SPY base EOD status ────────────────────────────────────────────────
    spy_log(broker)

    logger.banner(log, "MARKET CLOSE COMPLETE")


if __name__ == "__main__":
    run()
