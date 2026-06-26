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
import time

from core import logger, config
from core.broker import BrokerClient
from core.screener import screen
from core.notifier import send_trade_alert
from core.edge import circuit_breaker_tripped
from core import composite

log = logger.setup("market_open")
ET  = pytz.timezone("America/New_York")

DAY_START_PATH = os.path.join(config.STATE_DIR, "day_start_value.json")
MAX_BUYS       = 3


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


def run():
    config.validate()
    logger.banner(log, "MARKET OPEN — 9:30 AM ET")

    broker = BrokerClient()

    for attempt in range(30):
        if broker.is_market_open():
            log.info("Market is OPEN ✓")
            break
        time.sleep(10)
    else:
        log.error("Market closed — aborting")
        return

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

    log.info("Screening...")
    candidates = screen()
    log.info(f"Got {len(candidates)} candidates")

    if not candidates:
        log.info("No candidates — done")
        return

    passed = []
    for c in candidates:
        sym   = c["symbol"]
        price = c.get("price", 0)
        rs    = c.get("rs_vs_spy")
        gap   = c.get("gap_pct", 0)
        relv  = c.get("rel_volume", 0)
        score = c.get("score", 0)

        if sym in held:
            log.info(f"  ✗ {sym} SKIP — already holding")
            continue

        if not (5.0 <= price <= 500.0):
            log.info(f"  ✗ {sym} SKIP — price ${price:.2f} out of band")
            continue

        if rs is not None and rs < config.MIN_RS_RATING:
            log.info(f"  ✗ {sym} REJECT — RS {rs:+.1f}% < SPY+{config.MIN_RS_RATING}")
            continue

        if abs(gap) > config.MAX_GAP_PCT:
            log.info(f"  ✗ {sym} REJECT — gap {gap:+.1f}% > {config.MAX_GAP_PCT}%")
            continue

        if score < 20:
            log.info(f"  ✗ {sym} REJECT — score {score} below 20 floor")
            continue

        rs_str  = f"RS{rs:+.1f}%" if rs is not None else "RS n/a"
        log.info(f"  ✓ {sym} PASS — ${price:.2f} score={score} {rs_str} gap={gap:+.1f}% relvol={relv}")
        passed.append(c)

    log.info(f"Passed all skills: {len(passed)}/{len(candidates)}")

    if not passed:
        log.info("Nothing passed skills — done")
        return

    # ── COMPOSITE SCORING ───────────────────────────────────────────────────
    # Every GROUP A skill (Alpaca/no-API) contributes a 0-100 sub-score; GROUP B
    # (FMP) sub-scores fail gracefully to neutral 50 on any error/429. The
    # survivors are ranked by the regime-scaled composite, then the top `slots`
    # are bought. All existing filters above are unchanged; buy()/OCO untouched.
    log.info("Composite scoring (GROUP A skills + graceful GROUP B)...")
    passed_syms = [c["symbol"] for c in passed]
    ctx = composite.build_context(extra_symbols=passed_syms)
    log.info(f"Market regime score={ctx['regime_score']} → composite mult={ctx['regime_mult']} "
             f"| sector momentum: {ctx['sector_mom']}")

    bars_map = {}
    try:
        from core.screener import _fetch_bars
        bars_map = _fetch_bars(passed_syms, days=60)
    except Exception as e:
        log.warning(f"Composite bar fetch failed (non-blocking, bar-scores→neutral): {e}")

    scored = []
    for c in passed:
        result = composite.compute_composite(c, bars_map.get(c["symbol"]), ctx)
        c["composite"] = result["composite"]
        c["composite_final"] = result["final"]
        c["composite_breakdown"] = result["breakdown"]
        log.info(composite.format_breakdown(result))
        scored.append(c)

    scored.sort(key=lambda x: x.get("composite_final", 0), reverse=True)
    ranked = " > ".join(f"{c['symbol']}({c.get('composite_final', 0):.1f})" for c in scored)
    log.info(f"Composite ranking: {ranked}")
    passed = scored

    buys_taken = 0
    for c in passed[:slots]:
        sym    = c["symbol"]
        score  = c.get("score", 0)
        rs     = c.get("rs_vs_spy")
        comp     = c.get("composite_final", 0)
        size_pct = config.MAX_POSITION_SIZE_PCT
        amount   = pv * size_pct

        log.info(f"BUYING {sym} | composite={comp:.1f} (vcp={score}) | size={size_pct*100:.0f}% | ${amount:,.0f}")
        try:
            result = broker.buy(sym, dollar_amount=amount)
            log.info(f"✓ {sym} {result['qty']} sh @ ${result['price']:.2f} "
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
            buys_taken += 1
        except Exception as e:
            log.error(f"✗ {sym} buy failed: {e}")

    log.info(f"Market open complete | Buys taken: {buys_taken}")
    logger.banner(log, "MARKET OPEN COMPLETE")


if __name__ == "__main__":
    run()
