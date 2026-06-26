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

import json
import datetime
import pytz
from core import logger, config
from core.broker   import BrokerClient
from core.fmp      import get_quotes, get_market_breadth
from core.analyst  import review_open_positions, analyze_market_regime, score_vcp_candidates
from core.screener import screen
from core.edge     import should_pyramid, compute_trail_stop, circuit_breaker_tripped

log = logger.setup("midday")
ET  = pytz.timezone("America/New_York")

PYRAMID_STATE_PATH = os.path.join(config.STATE_DIR, "pyramided.json")
TODAY_BOUGHT_PATH  = os.path.join(config.STATE_DIR, "today_bought.json")


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


def _mark_bought(symbol: str) -> None:
    try:
        today = datetime.datetime.now(ET).date().isoformat()
        bought = _load_today_bought()
        bought.add(symbol)
        with open(TODAY_BOUGHT_PATH, "w") as f:
            json.dump({"date": today, "symbols": sorted(bought)}, f, indent=2)
    except Exception as e:
        log.warning(f"Failed to persist today_bought state: {e}")


def _load_pyramided() -> set:
    """Symbols already pyramided this cycle — prevents stacking adds."""
    try:
        with open(PYRAMID_STATE_PATH) as f:
            return set(json.load(f))
    except (FileNotFoundError, ValueError):
        return set()


def _save_pyramided(symbols: set) -> None:
    with open(PYRAMID_STATE_PATH, "w") as f:
        json.dump(sorted(symbols), f)


def run():
    config.validate()
    logger.banner(log, "MIDDAY REVIEW — 12:00 PM ET")

    broker = BrokerClient()

    # Market-open guard: a redeploy can trigger a catch-up run of this routine
    # outside RTH. Quotes are one-sided when closed (sizing/brackets break), and
    # we never want to place after-hours orders — so skip entirely if closed.
    if not broker.is_market_open():
        log.info("Market is CLOSED — skipping midday review (no after-hours trading)")
        return

    pv        = broker.portfolio_value()
    pos_count = broker.position_count()
    log.info(f"Portfolio: ${pv:,.2f} | Positions: {pos_count}")

    day_start_path = os.path.join(config.STATE_DIR, "day_start_value.json")
    day_start = pv
    try:
        import json as _json
        with open(day_start_path) as _f:
            _data = _json.load(_f)
        if _data.get("date") == datetime.datetime.now(ET).date().isoformat() and _data.get("value"):
            day_start = float(_data["value"])
    except (FileNotFoundError, ValueError, KeyError):
        pass

    if circuit_breaker_tripped(pv, day_start):
        day_pnl = (pv - day_start) / day_start * 100
        log.warning(f"CIRCUIT BREAKER: day P&L {day_pnl:+.2f}% — NO new entries, NO new pyramids")
        return
    log.info(f"Day P&L OK ({((pv - day_start) / day_start * 100):+.2f}%) — circuit breaker clear")

    breadth = get_market_breadth()
    regime  = analyze_market_regime(breadth)
    log.info(f"Midday regime: {regime['regime'].upper()} | Bias: {regime['trade_bias']}")

    positions = broker.get_positions()
    log.info(f"Open positions: {len(positions)}")

    # ── Repair: protect any naked position (no attached stop order) ───────────
    # Catches positions opened when a bracket was rejected (e.g. SHOP/NET/ZS).
    if positions:
        try:
            open_orders = broker.get_open_orders()
            stopped = {
                o.symbol for o in open_orders
                if "stop" in str(o.type).lower() and "sell" in str(o.side).lower()
            }
        except Exception as e:
            log.warning(f"Repair: could not fetch open orders ({e}) — skipping repair")
            stopped = None
        if stopped is not None:
            for p in positions:
                if p.symbol in stopped:
                    continue
                entry  = float(p.avg_entry_price)
                qty    = int(float(p.qty))
                if qty < 1:
                    continue
                # Anchor the stop on min(entry, current price): if the position
                # has already fallen past entry*(1-STOP_LOSS_PCT), a sell-stop
                # can't sit above market, so anchor on the current price instead
                # (otherwise Alpaca rejects "stop price must be < current price").
                anchor = entry
                try:
                    cur = broker.get_price(p.symbol)
                    if cur > 0:
                        anchor = min(entry, cur)
                except Exception as e:
                    log.warning(f"  {p.symbol}: price check failed ({e}) — anchoring on entry")
                stop   = round(anchor * (1 - config.STOP_LOSS_PCT), 2)
                target = round(entry  * (1 + config.TAKE_PROFIT_PCT), 2)
                log.info(f"  🛠️  {p.symbol}: naked position — attaching protection "
                         f"(entry=${entry:.2f} stop=${stop} target=${target})")
                broker.attach_stop_target(p.symbol, qty, stop, target)

    if not positions:
        log.info("No positions — skip position review")
    else:
        symbols    = [p.symbol for p in positions]
        quotes     = get_quotes(symbols)
        pos_data   = []
        pyramided  = _load_pyramided()
        pv         = broker.portfolio_value()
        trade_bias = regime.get("trade_bias", "moderate")

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

            # ── Intraday trail (#12): ratchet stop up on winners ─────────────
            if config.TRAIL_INTRADAY and current > entry:
                new_stop = compute_trail_stop(current, entry, stop)
                if new_stop > stop:
                    if broker.tighten_stop(sym, new_stop):
                        log.info(f"  🔒 {sym}: intraday trail stop ${stop} → ${new_stop}")
                        stop = new_stop

            # ── Pyramiding (#10): add to winners once past trigger ───────────
            if (trade_bias not in ("cash", "defensive")
                    and should_pyramid({"pnl_pct": pnl_pct, "pyramided": sym in pyramided})):
                add_amount = pv * config.MAX_POSITION_SIZE_PCT * 0.5
                try:
                    result = broker.buy(sym, dollar_amount=add_amount)
                    if result.get("blocked"):
                        log.warning(f"  ✗ {sym} pyramid blocked: {result.get('reason')}")
                    else:
                        pyramided.add(sym)
                        qty += result["qty"]
                        log.info(f"  ➕ {sym}: pyramided +{result['qty']} shares "
                                 f"@ ~${result['price']:.2f} (P&L +{pnl_pct:.1f}%)")
                except Exception as e:
                    log.error(f"  ✗ Pyramid {sym} failed: {e}")

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

        _save_pyramided(pyramided)

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
            already_bought_today = _load_today_bought()
            if already_bought_today:
                buys = [s for s in buys if s["symbol"] not in already_bought_today]
            log.info(f"  High-confidence midday setups: {len(buys)}")
            for s in buys[:3]:
                if s["symbol"] in already_bought_today:
                    log.info(f"  ✗ {s['symbol']:6} SKIP — already bought today")
                    continue
                log.info(f"  ⚡ {s['symbol']:6} score={s['score']} | {s['reason']}")
                try:
                    pv     = broker.portfolio_value()
                    amount = pv * config.MAX_POSITION_SIZE_PCT * 0.5
                    result = broker.buy(s["symbol"], dollar_amount=amount)
                    if result.get("blocked"):
                        log.warning(f"  ✗ {s['symbol']} midday buy blocked: {result.get('reason')}")
                        continue
                    if not result.get("stop_attached"):
                        log.error(f"  ✗ {s['symbol']} bought but stop NOT attached — flattening")
                        broker.sell(s["symbol"], qty=result["qty"])
                        continue
                    log.info(f"  ✓ Midday buy {s['symbol']}: {result['qty']} @ ~${result['price']:.2f}")
                    _mark_bought(s["symbol"])
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
