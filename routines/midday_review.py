from __future__ import annotations
"""
MIDDAY REVIEW ROUTINE — 12:00 PM ET, Mon-Fri
─────────────────────────────────────────────
1. Review all open positions (P&L, vs stop/target)
2. Claude: HOLD / SELL / TIGHTEN_STOP decisions
3. Execute any exits
4. Check for new high-quality setups (post-open volume data)
5. Cancel stale open orders
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz
from core import logger, config
from core.broker   import BrokerClient
from core.fmp      import get_quotes, get_market_breadth
from core.analyst  import review_open_positions, analyze_market_regime, score_vcp_candidates
from core.screener import screen

log = logger.setup("midday")
ET  = pytz.timezone("America/New_York")


def run():
    config.validate()
    logger.banner(log, "MIDDAY REVIEW — 12:00 PM ET")

    broker = BrokerClient()

    breadth = get_market_breadth()
    regime  = analyze_market_regime(breadth)
    log.info(f"Midday regime: {regime['regime'].upper()} | Bias: {regime['trade_bias']}")

    positions = broker.get_positions()
    log.info(f"Open positions: {len(positions)}")

    if not positions:
        log.info("No positions — skip position review")
    else:
        symbols  = [p.symbol for p in positions]
        quotes   = get_quotes(symbols)
        pos_data = []

        for p in positions:
            sym          = p.symbol
            entry        = float(p.avg_entry_price)
            current      = float(quotes.get(sym, {}).get("price", entry))
            qty          = int(float(p.qty))
            pnl_pct      = round((current - entry) / entry * 100, 2)
            unrealized   = float(p.unrealized_pl or 0)
            days_held    = 0

            stop   = round(entry * (1 - config.STOP_LOSS_PCT), 2)
            target = round(entry * (1 + config.TAKE_PROFIT_PCT), 2)

            log.info(f"  {sym:6} | entry=${entry:.2f} | now=${current:.2f} | "
                     f"P&L={pnl_pct:+.2f}% (${unrealized:+,.0f})")

            # ── Partial profit (#6): take 50% off at +PARTIAL_PROFIT_PCT ──────
            if pnl_pct >= config.PARTIAL_PROFIT_PCT * 100 and qty >= 2:
                trim_qty = max(1, int(qty * config.PARTIAL_PROFIT_SIZE))
                try:
                    broker.sell(sym, qty=trim_qty)
                    log.info(f"  💰 {sym}: partial profit — sold {trim_qty}/{qty} at +{pnl_pct:.1f}%")
                    new_trail_stop = round(current * (1 - config.TRAIL_STOP_PCT), 2)
                    broker.tighten_stop(sym, new_trail_stop)
                    log.info(f"  🔒 {sym}: trailing stop → ${new_trail_stop} on remaining {qty - trim_qty}")
                    qty -= trim_qty
                except Exception as e:
                    log.error(f"  ✗ Partial profit {sym} failed: {e}")

            pos_data.append({
                "symbol":       sym,
                "entry_price":  entry,
                "current_price": current,
                "qty":          qty,
                "pnl_pct":      pnl_pct,
                "unrealized_usd": unrealized,
                "days_held":    days_held,
                "stop":         stop,
                "target":       target,
            })

        log.info("── Claude: position review")
        decisions = review_open_positions(pos_data, regime["regime"])

        for d in decisions:
            sym    = d.get("symbol", "")
            action = d.get("action", "HOLD")
            reason = d.get("reason", "")
            new_stop = d.get("new_stop")

            log.info(f"  {sym:6} → {action} | {reason}")

            if action == "SELL":
                try:
                    broker.sell(sym)
                    log.info(f"  ✓ Sold {sym}")
                except Exception as e:
                    log.error(f"  ✗ Sell {sym} failed: {e}")

            elif action == "TIGHTEN_STOP" and new_stop:
                ok = broker.tighten_stop(sym, float(new_stop))
                if not ok:
                    log.warning("  %s: tighten_stop failed — no open stop order found", sym)

    open_orders = broker.get_open_orders()
    if open_orders:
        log.info(f"Open orders: {len(open_orders)} — cancelling stale")
        import datetime
        now = datetime.datetime.now(pytz.utc)
        for o in open_orders:
            try:
                submitted = o.submitted_at
                if submitted and (now - submitted.replace(tzinfo=pytz.utc)).total_seconds() > 1800:
                    broker.trade.cancel_order_by_id(o.id)
                    log.info(f"  Cancelled stale order: {o.symbol} {o.side}")
            except Exception as e:
                log.warning(f"  Cancel order fail: {e}")

    slots = config.MAX_OPEN_POSITIONS - broker.position_count()

    if slots > 0 and regime["trade_bias"] not in ["cash", "defensive"]:
        log.info(f"── Midday scan ({slots} slots available)")
        raw    = screen()
        top    = raw[:10]

        if top:
            scored = score_vcp_candidates(top)
            buys   = [s for s in scored if s.get("action") == "BUY" and s.get("score", 0) >= 75]
            log.info(f"  High-confidence midday setups: {len(buys)}")
            for s in buys[:3]:
                log.info(f"  ⚡ {s['symbol']:6} score={s['score']} | {s['reason']}")
                try:
                    pv     = broker.portfolio_value()
                    amount = pv * config.MAX_POSITION_SIZE_PCT * 0.5
                    result = broker.buy(s["symbol"], dollar_amount=amount)
                    log.info(f"  ✓ Midday buy {s['symbol']}: {result['qty']} @ ~${result['price']:.2f}")
                except Exception as e:
                    log.error(f"  ✗ Midday buy {s['symbol']} failed: {e}")
    else:
        log.info(f"No midday scan (slots={slots}, bias={regime['trade_bias']})")

    positions_after = broker.get_positions()
    total_unrealized = sum(float(p.unrealized_pl or 0) for p in positions_after)
    log.info(f"── Midday summary")
    log.info(f"  Positions: {len(positions_after)}")
    log.info(f"  Total unrealized P&L: ${total_unrealized:+,.2f}")

    logger.banner(log, "MIDDAY REVIEW COMPLETE")


if __name__ == "__main__":
    run()
