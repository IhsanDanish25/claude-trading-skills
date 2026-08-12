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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
import json

import pytz

from core import config, logger
from core.analyst import analyze_market_regime, detect_ftd, review_open_positions
from core.broker import BrokerClient
from core.fmp import get_daily_bars, get_market_breadth, get_quotes
from core.notifier import send_eod_summary, send_trade_alert
from core.order_utils import order_field
from core.pead_tracker import get_all as pead_all
from core.pead_tracker import get_expired
from core.pead_tracker import remove_position as pead_untrack
from core.spy_base import is_base_symbol
from core.spy_base import log_status as spy_log

log = logger.setup("market_close")
ET = pytz.timezone("America/New_York")

CLOSE_EXIT_THRESHOLD = -0.03  # Force exit if P&L < -3%
TRADE_LOG_PATH = os.path.join(config.STATE_DIR, "trade_log.jsonl")
SKIPPED_ROUTINES_FILE = os.path.join(config.STATE_DIR, "skipped_routines.json")


def _todays_trades(today: str) -> list[dict]:
    """Read state/trade_log.jsonl entries stamped today, for the EOD summary.

    Only real fills (side == buy/sell) — order_skipped/blocked entries have no
    qty/price and used to leak into this list, rendering as garbled "? sh @
    $0.00" rows in the EOD email (see 2026-08-12 MSFT/JPM/GE/C spread-gate
    incident). Blocked attempts are returned separately by _todays_blocked."""
    trades = []
    try:
        with open(TRADE_LOG_PATH) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not str(entry.get("ts", "")).startswith(today):
                    continue
                if entry.get("side") in ("buy", "sell"):
                    trades.append(entry)
    except FileNotFoundError:
        pass
    return trades


def _todays_blocked(today: str) -> list[dict]:
    """Read state/trade_log.jsonl entries stamped today with event=="order_skipped" —
    orders blocked before ever reaching Alpaca (spread gate, buying-power clamp,
    stop-attach failure, etc). Kept separate from _todays_trades so the EOD email
    can render them as their own explicit BLOCKED rows with the real symbol/reason,
    instead of fabricated $0.00/qty=0 trade rows."""
    blocked = []
    try:
        with open(TRADE_LOG_PATH) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not str(entry.get("ts", "")).startswith(today):
                    continue
                if entry.get("event") == "order_skipped":
                    blocked.append(entry)
    except FileNotFoundError:
        pass
    return blocked


def _todays_skipped_routines(today: str) -> list[dict]:
    """Read scheduler.py's persisted stale-catchup skips for today, so a
    routine that silently never ran (e.g. worker down past the 2h catch-up
    cap) shows up in the EOD email instead of only in Railway logs."""
    try:
        with open(SKIPPED_ROUTINES_FILE) as f:
            data = json.load(f)
        if data.get("date") == today:
            return data.get("skipped", [])
    except (FileNotFoundError, ValueError, KeyError):
        pass
    return []


# ── 14:45 ET Secondary Trail Eval ─────────────────────────────────────────
# Second trailing-stop sweep of the day (first at 12:00 noon in midday_review).
# Handles winners that ran hard post-noon and would otherwise give back 6-8%.
# Only fires between 14:45 and 15:50 ET to avoid interfering with the
# 15:00 close auction and the open-order cancellation that follows.


def _build_stop_map(open_orders) -> dict:
    """sym -> current stop price for open sell stop orders. Uses order_field
    because str(enum) is 'OrderType.STOP' — the old comparison left this map
    permanently empty. Substring match (not "==") because attach_stop_target
    always sets a limit_price alongside stop_price, so Alpaca classifies
    these orders as "stop_limit", not "stop" — an exact match here left the
    map empty for every real position's protective order."""
    stop_map = {}
    for o in open_orders or []:
        try:
            sp = getattr(o, "stop_price", None)
            if (
                "stop" in order_field(o, "type")
                and order_field(o, "side") == "sell"
                and isinstance(sp, (int, float))
            ):
                stop_map[o.symbol] = float(sp)
        except Exception:
            pass
    return stop_map


def _late_trail(broker: BrokerClient) -> int:
    """14:45 ET secondary trailing-stop ratchet. Returns count of stops tightened."""
    from core.edge import compute_trail_stop

    now_et = datetime.datetime.now(ET)
    cutoff = datetime.time(14, 45)
    expire = datetime.time(15, 50)
    if not (cutoff <= now_et.time() < expire):
        return 0

    positions = broker.get_positions()
    open_orders = broker.get_open_orders()
    stop_map = _build_stop_map(open_orders)

    quotes = get_quotes([p.symbol for p in positions])
    tightened = 0
    for pos in positions:
        sym = pos.symbol
        if is_base_symbol(sym):
            continue  # SPY base carries no protective stop by design
        entry = float(pos.avg_entry_price)
        cur = float(quotes.get(sym, {}).get("price", entry))
        if cur <= entry:
            continue  # losers — OCO handles the stop

        cur_stop = stop_map.get(sym, round(entry * (1 - config.STOP_LOSS_PCT), 2))
        default_stop = round(entry * (1 - config.STOP_LOSS_PCT), 2)
        base_stop = max(cur_stop, default_stop)
        new_stop = compute_trail_stop(cur, entry, base_stop)
        if new_stop > base_stop + 0.01:  # only tighten if materially better
            if broker.tighten_stop(sym, new_stop):
                log.info(
                    f"  14:45 TRAIL {sym}: ${base_stop:.2f} → ${new_stop:.2f}  (now ${cur:.2f})"
                )
                tightened += 1
    log.info(f"  14:45 trail eval: {tightened} stops tightened")
    return tightened


def _resolve_eod_stop(base_stop: float, action: str, new_stop) -> float:
    """Stop price to re-attach after the EOD cancel-all, given Claude's
    decision. TIGHTEN_STOP only wins if new_stop is a real number tighter
    (higher) than the entry-based default — a missing/garbage/looser
    new_stop falls back to base_stop rather than weakening protection."""
    if action == "TIGHTEN_STOP" and isinstance(new_stop, (int, float)) and new_stop > base_stop:
        return round(float(new_stop), 2)
    return base_stop


def run():
    config.validate()
    logger.banner(log, "MARKET CLOSE — 3:00 PM ET")

    broker = BrokerClient()
    today = datetime.date.today().isoformat()

    # ── 14:45 secondary trail eval (fires up to 15 min before close) ─────────
    _late_trail(broker)

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
                    pos = broker.get_position(sym)
                    qty = float(pos.qty) if pos else 0
                    cur_price = broker.get_price(sym)
                    broker.close_position(sym)
                    pead_untrack(sym)
                    send_trade_alert(
                        action="SELL",
                        ticker=sym,
                        shares=qty,
                        price=cur_price,
                        stop=0,
                        target=0,
                        reason=f"PEAD time-exit: {age}d held (max {hold}d)",
                    )
                    log.info(f"  ✓ PEAD closed {sym} after {age} days")
                except Exception as e:
                    log.error(f"  ✗ PEAD close {sym} failed: {e}")
        else:
            for sym, info in pead_positions.items():
                from core.pead_tracker import position_age

                age = position_age(sym)
                log.info(
                    f"  PEAD {sym}: day {age}/{info.get('hold_days', 60)} "
                    f"(surprise={info.get('surprise_pct', '?')}%)"
                )

    # ── Position final review ─────────────────────────────────────────────────
    positions = broker.get_positions()
    log.info(f"── Positions at close: {len(positions)}")

    force_close = []  # symbols force-closed below -3%; referenced in EOD summary
    position_signals = []  # Claude's HOLD/SELL/TIGHTEN_STOP per position; referenced in EOD summary

    breadth = get_market_breadth()
    regime = analyze_market_regime(breadth)
    log.info(f"EOD regime: {regime['regime'].upper()} | Bias: {regime['trade_bias']}")

    if not positions:
        log.info("No positions to review")
    else:
        symbols = [p.symbol for p in positions]
        quotes = get_quotes(symbols)

        pos_data = []

        for p in positions:
            sym = p.symbol
            # SPY base is managed by spy_base — exclude it from force-close,
            # the cash-bias close-all, and the Claude EOD review. Closing the
            # ~full-portfolio base on a -3% day is not a trade exit.
            if is_base_symbol(sym):
                log.info(f"  {sym:6} | SPY base holding — excluded from EOD review")
                continue
            entry = float(p.avg_entry_price)
            current = float(quotes.get(sym, {}).get("price", entry))
            qty = float(p.qty)
            pnl_pct = (current - entry) / entry
            unrealized = float(p.unrealized_pl or 0)

            log.info(
                f"  {sym:6} | ${entry:.2f} → ${current:.2f} | "
                f"{pnl_pct * 100:+.2f}% | ${unrealized:+,.0f}"
            )

            # Force-close deep losers before market shuts
            if pnl_pct <= CLOSE_EXIT_THRESHOLD:
                log.warning(f"  ⚠️  {sym} below -3% threshold — force close")
                force_close.append(sym)
            else:
                pos_data.append(
                    {
                        "symbol": sym,
                        "entry_price": entry,
                        "current_price": current,
                        "qty": qty,
                        "pnl_pct": pnl_pct * 100,
                        "unrealized_usd": unrealized,
                        "days_held": 1,
                        "stop": round(entry * (1 - config.STOP_LOSS_PCT), 2),
                        "target": round(entry * (1 + config.TAKE_PROFIT_PCT), 2),
                    }
                )

        # Force-close deep losers before market shuts
        for sym in force_close:
            try:
                pos = broker.get_position(sym)
                close_qty = float(pos.qty) if pos else 0
                cur_price = broker.get_price(sym)
                broker.close_position(sym)
                log.info(f"  ✓ Force-closed {sym} {close_qty} shares")
                send_trade_alert(
                    "SELL", sym, close_qty, cur_price, 0, 0, reason="Force-closed: -3% threshold"
                )
            except Exception as e:
                log.error(f"  ✗ Close {sym} failed: {e}")

        # Claude review on remaining positions. The discretionary cash-regime
        # close-all (up to 3/day, unranked) was removed — EARNMOM/INSIDER now
        # carry a real OCO take-profit, and de-risking during a cash/defensive
        # regime is handled upstream by market_open.py's slot-gating (no new
        # entries), not by liquidating existing protected positions here.
        if pos_data:
            log.info(f"── Claude: EOD position review ({len(pos_data)} positions)")
            decisions = review_open_positions(pos_data, regime["regime"])
            position_signals = decisions
            pos_by_symbol = {p["symbol"]: p for p in pos_data}
            for d in decisions:
                sym = d.get("symbol", "")
                action = d.get("action", "HOLD")
                reason = d.get("reason", "")
                log.info(f"  {sym:6} → {action} | {reason}")

                if action == "SELL":
                    try:
                        broker.sell(sym)
                        log.info(f"  ✓ EOD sold {sym}")
                    except Exception as e:
                        log.error(f"  ✗ {e}")
                    continue

                # HOLD / TIGHTEN_STOP: the cancel-all-orders step above just
                # stripped this position's stop-loss, and no later routine
                # re-arms one until midday_review at noon tomorrow — a ~21hr
                # unprotected window spanning the overnight gap and the next
                # session's open. Re-attach immediately so nothing rides
                # naked overnight.
                p_info = pos_by_symbol.get(sym)
                if not p_info:
                    continue
                qty = p_info["qty"]
                target = p_info["target"]
                stop = _resolve_eod_stop(p_info["stop"], action, d.get("new_stop"))
                stop_attached, _ = broker.attach_stop_target(sym, qty, stop, target)
                if stop_attached:
                    log.info(f"  ✓ {sym}: protection re-attached (stop=${stop} target=${target})")
                else:
                    log.error(
                        f"  ✗ {sym}: stop-loss re-attach FAILED after EOD cancel-all "
                        f"(verified against Alpaca) — flattening to avoid an unprotected "
                        f"overnight position"
                    )
                    try:
                        broker.sell(sym, qty=qty)
                        send_trade_alert(
                            action="FLATTEN",
                            ticker=sym,
                            shares=qty,
                            price=p_info.get("current_price", 0),
                            stop=stop,
                            target=target,
                            reason="EOD stop-loss re-attach failed — closed to avoid naked overnight exposure",
                        )
                    except Exception as e:
                        log.error(
                            f"  ✗ {sym}: flatten-on-attach-failure ALSO failed: {e} "
                            f"— position remains unprotected overnight, needs manual review"
                        )

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
    pv = float(acct.portfolio_value)
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
        "date": today,
        "portfolio_value": pv,
        "cash": cash,
        "positions_held": len(final_positions),
        "unrealized_pnl": total_unrealized,
        "regime": regime.get("regime", "unknown"),
        "trade_bias": regime.get("trade_bias", "unknown"),
        "ftd": ftd_result,
        "spy_change_pct": breadth.get("spy_change_pct", 0),
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
        trades_today=_todays_trades(today),
        blocked_today=_todays_blocked(today),
        skipped_routines=_todays_skipped_routines(today),
        positions=position_signals,
    )

    # ── SPY base EOD status ────────────────────────────────────────────────
    spy_log(broker)

    logger.banner(log, "MARKET CLOSE COMPLETE")


if __name__ == "__main__":
    run()
