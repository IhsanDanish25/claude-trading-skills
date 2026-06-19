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

log = logger.setup("market_close")
ET  = pytz.timezone("America/New_York")

CLOSE_EXIT_THRESHOLD = -0.03   # Force exit if P&L < -3%
MIN_HOLD_TO_KEEP     = -0.01   # Keep if P&L > -1% (let it breathe overnight)


def run():
    logger.banner(log, "MARKET CLOSE — 3:00 PM ET")

    broker = BrokerClient()
    today  = datetime.date.today().isoformat()

    # ── Cancel all open day orders ────────────────────────────────────────────
    log.info("── Cancelling open orders")
    try:
        broker.cancel_all_orders()
    except Exception as e:
        log.warning(f"Cancel orders: {e}")

    # ── Position final review ─────────────────────────────────────────────────
    positions = broker.get_positions()
    log.info(f"── Positions at close: {len(positions)}")

    if not positions:
        log.info("No positions to review")
    else:
        symbols = [p.symbol for p in positions]
        quotes  = get_quotes(symbols)

        # Market regime (close-of-day)
        breadth = get_market_breadth()
        regime  = analyze_market_regime(breadth)

        pos_data = []
        force_close = []

        for p in positions:
            sym      = p.symbol
            entry    = float(p.avg_entry_price)
            current  = float(quotes.get(sym, {}).get("price", entry))
            qty      = int(p.qty)
            pnl_pct  = (current - entry) / entry
            unrealized = float(p.unrealized_pl)

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
                q = quotes.get(sym, {})
                price = float(q.get("price", 0))
                send_trade_alert("SELL", sym, 0, price, 0, 0, reason="Force-closed: -3% threshold")
            except Exception as e:
                log.error(f"  ✗ Close {sym} failed: {e}")

        # Claude review on remaining
        if pos_data and regime["trade_bias"] == "cash":
            log.warning("Cash regime — closing ALL remaining positions")
            for pd in pos_data:
                try:
                    broker.close_position(pd["symbol"])
                    log.info(f"  ✓ Cash-regime close: {pd['symbol']}")
                except Exception as e:
                    log.error(f"  ✗ {e}")

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
    total_unrealized = sum(float(p.unrealized_pl) for p in final_positions)

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

    log_path = f"/tmp/daily_log_{today}.json"
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

    logger.banner(log, "MARKET CLOSE COMPLETE")


if __name__ == "__main__":
    run()
