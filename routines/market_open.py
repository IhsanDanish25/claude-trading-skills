from __future__ import annotations
"""
MARKET-OPEN ROUTINE — 9:30 AM ET, Mon-Fri
──────────────────────────────────────────
1. Load pre-market watchlist (or re-screen if missing)
2. Confirm market is open
3. Filter by opening volume + price action
4. Execute buys on top AI-scored setups
5. Set alerts / log orders
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import datetime
import pytz
import time

from core import logger, config
from core.broker   import BrokerClient
from core.fmp      import get_quotes, get_market_breadth
from core.analyst  import analyze_market_regime, score_vcp_candidates
from core.screener import screen
from core.notifier import send_trade_alert
from core.edge     import (is_entry_window, apply_edge_filters,
                           strong_sectors, rs_rating, _return_pct,
                           gap_ok, earnings_clear, sector_concentration_ok,
                           circuit_breaker_tripped)
from core.analyst  import detect_ftd
from core.fmp      import get_daily_bars

log = logger.setup("market_open")
ET  = pytz.timezone("America/New_York")

WATCHLIST_PATH    = os.path.join(config.STATE_DIR, "pre_market_watchlist.json")
DAY_START_PATH    = os.path.join(config.STATE_DIR, "day_start_value.json")
MAX_BUYS          = 3   # max new entries at open


def load_day_start_value(current_pv: float) -> float:
    """Return today's starting portfolio value, recording it on first run of the
    day. Used by the circuit breaker to measure intraday drawdown."""
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


def load_watchlist() -> dict:
    try:
        with open(WATCHLIST_PATH) as f:
            data = json.load(f)
        age_mins = (datetime.datetime.now(ET) -
                    datetime.datetime.fromisoformat(data["generated"])
                    .astimezone(ET)).total_seconds() / 60
        if age_mins > 180:
            log.warning(f"Watchlist stale ({age_mins:.0f} min old) — rescreening")
            raise FileNotFoundError
        log.info(f"Watchlist loaded ({age_mins:.0f} min old)")
        return data
    except (FileNotFoundError, KeyError, Exception) as e:
        log.warning(f"No watchlist ({e}) — running fresh screen")
        return None


def run():
    config.validate()
    logger.banner(log, "MARKET OPEN — 9:30 AM ET")

    broker = BrokerClient()

    for attempt in range(30):
        if broker.is_market_open():
            log.info("Market is OPEN ✓")
            break
        if attempt % 6 == 0:
            log.info(f"Market not yet open, waiting... ({attempt * 10 // 60} min elapsed)")
        time.sleep(10)
    else:
        log.error("Market still closed after 5 min wait — aborting")
        return

    # ── Entry timing gate (upgrade #1) ────────────────────────────────────────
    allowed, why = is_entry_window()
    if not allowed:
        log.warning(f"Entry blocked: {why}")
        return
    log.info(f"Entry timing: {why}")

    watchlist_data = load_watchlist()

    if watchlist_data:
        regime   = watchlist_data.get("regime", {})
        buy_list = watchlist_data.get("buy_list", [])
    else:
        breadth = get_market_breadth()
        regime  = analyze_market_regime(breadth)
        raw     = screen()
        buy_list = score_vcp_candidates(raw[:15])
        buy_list = [s for s in buy_list if s.get("action") == "BUY"]

    trade_bias = regime.get("trade_bias", "moderate")
    log.info(f"Regime: {regime.get('regime','?').upper()} | Bias: {trade_bias}")

    # ── FTD bottom-catch check (upgrade #2) ───────────────────────────────────
    ftd_detected = False
    if trade_bias == "cash" and config.ALLOW_FTD_BOTTOM_BUY:
        try:
            spy_bars = get_daily_bars("SPY", days=20)
            ftd = detect_ftd(spy_bars)
            ftd_detected = ftd.get("ftd_detected", False)
            if ftd_detected:
                log.info(f"FTD DETECTED ({ftd.get('ftd_date')}) — defensive bottom-catch enabled")
        except Exception as e:
            log.warning(f"FTD check failed: {e}")

    if trade_bias == "cash" and not ftd_detected:
        log.warning("Cash bias, no FTD — NO new entries")
        return

    pv        = broker.portfolio_value()
    pos_count = broker.position_count()
    slots     = config.MAX_OPEN_POSITIONS - pos_count

    log.info(f"Portfolio: ${pv:,.2f} | Positions: {pos_count} | Slots: {slots}")

    # ── Circuit breaker (#11) ─────────────────────────────────────────────────
    day_start = load_day_start_value(pv)
    if circuit_breaker_tripped(pv, day_start):
        day_pnl = (pv - day_start) / day_start * 100
        log.warning(f"CIRCUIT BREAKER tripped: day P&L {day_pnl:+.2f}% ≤ "
                    f"-{config.CIRCUIT_BREAKER_PCT*100:.1f}% — NO new entries")
        return

    if slots <= 0:
        log.info("No slots available — no buys")
        return

    if not buy_list:
        log.info("No BUY candidates — nothing to execute")
        return

    symbols     = [c["symbol"] for c in buy_list[:10]]
    live_quotes = get_quotes(symbols)

    now_et = datetime.datetime.now(ET)
    market_open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    mins_since_open = max(1, (now_et - market_open_t).total_seconds() / 60)
    day_fraction = mins_since_open / 390

    confirmed = []
    for candidate in buy_list:
        sym = candidate["symbol"]
        q   = live_quotes.get(sym, {})
        if not q:
            continue

        live_price  = float(q.get("price", 0))
        live_vol    = float(q.get("volume", 0))
        avg_vol     = float(q.get("avgVolume", 1))

        estimated_daily_vol = live_vol / day_fraction if day_fraction > 0 else live_vol
        adj_rel_v = estimated_daily_vol / avg_vol if avg_vol > 0 else 0

        passes_vol   = adj_rel_v >= config.MIN_RELATIVE_VOLUME or live_vol > 10_000
        passes_price = config.MIN_PRICE <= live_price <= config.MAX_PRICE

        log.info(f"  {sym}: ${live_price:.2f} | adj_rel_vol={adj_rel_v:.2f} "
                 f"({mins_since_open:.0f}min in) | vol={passes_vol} price={passes_price}")

        if passes_vol and passes_price:
            confirmed.append({**candidate, "live_price": live_price, "adj_rel_vol": adj_rel_v})

    log.info(f"Confirmed after live filter: {len(confirmed)}")

    # ── Edge filters: RS (#3) + sector (#4) + volume (#5) + sizing (#2) ───────
    spy_return = _return_pct("SPY")
    sector_list = strong_sectors(top_n=4) if config.STRONG_SECTORS_ONLY else []
    log.info(f"SPY 1mo return: {spy_return}% | Strong sectors: {sector_list}")

    edge_passed = []
    for c in confirmed:
        sym     = c["symbol"]
        verdict = apply_edge_filters(
            candidate=c,
            regime_bias=trade_bias,
            ftd_detected=ftd_detected,
            spy_return=spy_return,
            strong_sector_list=sector_list,
        )
        if not verdict["pass"]:
            log.info(f"  ✗ {sym} REJECT | {', '.join(verdict['reasons'])}")
            continue

        # ── Gap guard (#7) ────────────────────────────────────────────────────
        g_ok, gap = gap_ok(sym, c.get("live_price", 0))
        if not g_ok:
            log.info(f"  ✗ {sym} REJECT | gap {gap:+.1f}% > {config.MAX_GAP_PCT}% vs prior close")
            continue

        # ── Earnings blackout (#8) ────────────────────────────────────────────
        e_ok, edate = earnings_clear(sym)
        if not e_ok:
            log.info(f"  ✗ {sym} REJECT | earnings {edate} within {config.EARNINGS_BLACKOUT_DAYS}d blackout")
            continue

        # ── Sector concentration cap (#9) ─────────────────────────────────────
        s_ok, scount = sector_concentration_ok(c.get("sector", ""), edge_passed)
        if not s_ok:
            log.info(f"  ✗ {sym} REJECT | sector '{c.get('sector','')}' at cap "
                     f"({scount}/{config.MAX_PER_SECTOR})")
            continue

        if gap is not None:
            verdict["reasons"].append(f"gap {gap:+.1f}%")
        if edate:
            verdict["reasons"].append(f"earnings {edate} clear")
        edge_passed.append({**c, "_edge": verdict})
        log.info(f"  ✓ {sym} PASS edge | {', '.join(verdict['reasons'])}")

    log.info(f"Passed all edge filters: {len(edge_passed)}/{len(confirmed)}")

    buys_taken = 0
    max_buys   = min(MAX_BUYS, slots)
    if trade_bias == "defensive" or ftd_detected:
        max_buys = min(1, max_buys)

    for c in edge_passed[:max_buys]:
        sym      = c["symbol"]
        score    = c.get("score", 0)
        reason   = c.get("reason", "")
        size_pct = c["_edge"]["size_pct"]
        rs       = c["_edge"]["rs"]
        amount   = pv * size_pct

        log.info(f"  Buying {sym} | score={score} | RS={rs} | "
                 f"size={size_pct*100:.1f}% | ${amount:,.0f} | {reason}")

        try:
            result = broker.buy(sym, dollar_amount=amount)
            log.info(f"  ✓ Order placed: {result['qty']} shares @ ~${result['price']:.2f} | "
                     f"SL={result['stop']} TP={result['target']}")
            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=result["target"],
                reason=f"{reason} | RS{rs:+.0f} | {', '.join(c['_edge']['reasons'])}" if rs else reason,
            )
            buys_taken += 1
        except Exception as e:
            log.error(f"  ✗ Buy {sym} failed: {e}")

    log.info(f"Market open complete | Buys taken: {buys_taken}")
    logger.banner(log, "MARKET OPEN COMPLETE")


if __name__ == "__main__":
    run()
