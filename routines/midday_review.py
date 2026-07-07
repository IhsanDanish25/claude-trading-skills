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
import time
import pytz
from core import logger, config
from core.broker   import BrokerClient
from core.fmp      import get_quotes, get_market_breadth
from core.analyst  import review_open_positions, analyze_market_regime, score_vcp_candidates
from core.screener import screen
from core.edge     import should_pyramid, compute_trail_stop, circuit_breaker_tripped
from core.spy_base import rebalance_to_spy, log_status as spy_log, is_base_symbol
from core.order_utils import order_field as _order_field
from core.notifier import send_trade_alert

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


def _clear_base_protection(broker, open_orders, symbol: str) -> int:
    """Cancel open sell orders (legacy protection OCOs) on the SPY base
    holding. An OCO holds the full share qty, so Alpaca rejects every base
    rebalance sell with 40310000 until the OCO is gone. Returns the number
    of orders cancelled; failures are logged and skipped."""
    cancelled = 0
    for o in open_orders or []:
        try:
            if o.symbol == symbol and _order_field(o, "side") == "sell":
                broker.trade.cancel_order_by_id(o.id)
                cancelled += 1
        except Exception as e:
            log.warning(f"  {symbol}: base protection cancel failed: {e}")
    return cancelled


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

    # ── Repair: ensure every position has a stop order ──────────────────────
    # Only cancel orphaned orders (limiting old positions we no longer hold).
    # NEVER cancel existing OCO protection on positions we currently hold —
    # the cancel+reattach cycle can race against tighten_stop and cause "no
    # open stop order" failures when Alpaca re-propagates the new OCO.
    #
    # Stale-stop guard: if a position already has a stop-loss order (trailing
    # stop was applied at 12:00 noon), DON'T replace it — the midday trail
    # tightens it further as the stock runs. Blindly overwriting it with entry-
    # priced STOP_LOSS_PCT throws away 6-8% of trailing gain.
    if positions:
        open_orders = broker.get_open_orders()
        stop_orders = set()
        if open_orders:
            for o in open_orders:
                try:
                    # _order_field: str(enum) is 'OrderType.STOP', so the old
                    # str().lower() comparison never matched and this guard
                    # was dead — every midday re-attached protection OCOs.
                    otype = _order_field(o, "type")
                    oside = _order_field(o, "side")
                    if otype in ("stop", "trailing_stop") and oside == "sell":
                        stop_orders.add(o.symbol)
                except Exception:
                    pass

            for o in open_orders:
                try:
                    if o.symbol not in {p.symbol for p in positions}:
                        broker.trade.cancel_order_by_id(o.id)
                        log.info(f"  Cancelled orphan order: {o.symbol} {o.side}")
                except Exception as e:
                    log.warning(f"  Cancel order fail: {e}")

        for p in positions:
            # SPY base is a cash-parking holding managed by spy_base — never
            # attach a protection OCO to it. Also cancel any legacy OCO so
            # base rebalance sells stop bouncing off held_for_orders.
            if is_base_symbol(p.symbol):
                n = _clear_base_protection(broker, open_orders, p.symbol)
                log.info(f"  {p.symbol}: SPY base holding — no protection OCO"
                         + (f" ({n} legacy sell orders cancelled)" if n else ""))
                continue

            # Stale-stop guard: skip re-attach if position already holds a stop
            # (trailing stop from 12:00 noon trail eval is in place).
            if p.symbol in stop_orders:
                log.info(f"  {p.symbol}: stop-loss order already live — skip re-attach")
                continue

            entry = float(p.avg_entry_price)
            qty = int(float(p.qty))
            if qty < 1:
                continue
            anchor = entry
            try:
                cur = broker.get_price(p.symbol)
                if cur > 0:
                    anchor = min(entry, cur)
            except Exception as e:
                log.warning(f"  {p.symbol}: price check failed ({e}) — anchoring on entry")
            stop = round(anchor * (1 - config.STOP_LOSS_PCT), 2)
            target = round(entry * (1 + config.TAKE_PROFIT_PCT), 2)
            log.info(f"  Attaching protection: {p.symbol} "
                     f"(entry=${entry:.2f} stop=${stop} target=${target})")
            broker.attach_stop_target(p.symbol, qty, stop, target)

        # Brief pause to let Alpaca propagate the new OCO orders before
        # we try to update them in the review loop below.
        time.sleep(2)

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
            # SPY base is managed by spy_base — exclude it from partial
            # profit trims, intraday trailing, and the Claude review so a
            # SELL decision can't liquidate the cash-parking base.
            if is_base_symbol(sym):
                log.info(f"  {sym:6} | SPY base holding — excluded from review")
                continue
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
                    cur_price = quotes.get(sym, {}).get("price", 0)
                    broker.sell(sym, qty=trim_qty)
                    send_trade_alert(
                        action="SELL",
                        ticker=sym,
                        shares=trim_qty,
                        price=cur_price,
                        stop=0,
                        target=0,
                        reason=f"Partial profit at +{pnl_pct:.1f}% — trimmed {trim_qty} of {qty} shares",
                    )
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
                        send_trade_alert(
                            action="BUY",
                            ticker=sym,
                            shares=result["qty"],
                            price=result["price"],
                            stop=0,
                            target=0,
                            reason=f"PYRAMID — adding {result['qty']} shares at +{pnl_pct:.1f}% P&L",
                        )
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
                    pos = broker.get_position(sym)
                    qty = int(float(pos.qty)) if pos else 0
                    cur_price = quotes.get(sym, {}).get("price", 0)
                    broker.sell(sym)
                    send_trade_alert(
                        action="SELL",
                        ticker=sym,
                        shares=qty,
                        price=cur_price,
                        stop=0,
                        target=0,
                        reason=f"Claude midday decision: {reason}",
                    )
                    log.info(f"  ✓ Sold {sym}")
                except Exception as e:
                    log.error(f"  ✗ Sell {sym} failed: {e}")

            elif action == "TIGHTEN_STOP" and new_stop:
                ok = broker.tighten_stop(sym, float(new_stop))
                if not ok:
                    log.warning("  %s: tighten_stop failed — no open stop order found", sym)

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
                    send_trade_alert(
                        action="BUY",
                        ticker=s["symbol"],
                        shares=result["qty"],
                        price=result["price"],
                        stop=result.get("stop", 0),
                        target=result.get("target", 0),
                        reason=f"Midday VCP setup — score={s.get('score', '?')}",
                    )
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

    # ── SPY base rebalance ─────────────────────────────────────────────────
    spy_log(broker)
    spy_result = rebalance_to_spy(broker)
    if spy_result["action"] not in ("none", "disabled"):
        log.info(f"SPY base midday: {spy_result['action']} {spy_result.get('qty', 0)} shares")

    logger.banner(log, "MIDDAY REVIEW COMPLETE")


if __name__ == "__main__":
    run()
