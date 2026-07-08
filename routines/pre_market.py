from __future__ import annotations
"""
PRE-MARKET ROUTINE — 6:00 AM ET, Mon-Fri
────────────────────────────────────────
1. Market regime check (FMP breadth + Claude)
2. Economic calendar scan (high-impact events today)
3. VCP screener → AI scoring → build watchlist
4. Account health check
5. Log plan for day
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import datetime
import pytz

from core import logger, config
from core.broker    import BrokerClient
from core.fmp       import get_market_breadth, get_economic_calendar, get_news
from core.analyst   import analyze_market_regime, score_vcp_candidates, detect_ftd
from core.screener  import screen
from core.notifier  import send_premarket_brief

log  = logger.setup("pre_market")
ET   = pytz.timezone("America/New_York")


def run():
    config.validate()
    logger.banner(log, "PRE-MARKET — 6:00 AM ET")

    broker = BrokerClient()

    # ── 1. Account snapshot ───────────────────────────────────────────────────
    acct = broker.get_account()
    pv   = float(acct.portfolio_value)
    cash = float(acct.cash)
    bp   = float(acct.buying_power)
    pos_count = broker.position_count()

    log.info(f"Portfolio: ${pv:,.2f} | Cash: ${cash:,.2f} | BP: ${bp:,.2f}")
    log.info(f"Open positions: {pos_count}/{config.MAX_OPEN_POSITIONS}")

    # ── Anchor equity for CircuitBreaker daily-loss tracking ─────────────────
    day_start_path = os.path.join(config.STATE_DIR, "day_start_value.json")
    try:
        with open(day_start_path, "w") as f:
            json.dump({"date": datetime.date.today().isoformat(), "value": pv}, f)
        log.info(f"Day-start anchor saved: {pv:,.2f}")
    except Exception as e:
        log.warning(f"Could not write day-start value: {e}")

    # ── 2. Economic calendar ──────────────────────────────────────────────────
    log.info("── Economic calendar (next 3 days)")
    calendar = get_economic_calendar(days_ahead=3)
    high_impact = [e for e in calendar if e.get("impact", "") == "High"]
    if high_impact:
        for e in high_impact[:5]:
            log.info(f"  ⚡ {e.get('date','')} | {e.get('event','')} | {e.get('country','')}")
    else:
        log.info("  No high-impact events found")

    # ── 3. Market breadth + regime ────────────────────────────────────────────
    log.info("── Market breadth")
    breadth = get_market_breadth()
    log.info(f"  SPY: {breadth.get('spy_change_pct', 0):+.2f}%")
    log.info(f"  QQQ: {breadth.get('qqq_change_pct', 0):+.2f}%")
    log.info(f"  IWM: {breadth.get('iwm_change_pct', 0):+.2f}%")

    # Top 3 sectors
    sectors = breadth.get("sector_perf", {})
    if sectors:
        top = sorted(sectors.items(), key=lambda x: x[1], reverse=True)[:3]
        bot = sorted(sectors.items(), key=lambda x: x[1])[:3]
        log.info(f"  Top sectors: {top}")
        log.info(f"  Weak sectors: {bot}")

    log.info("── Claude: regime analysis")
    regime = analyze_market_regime({
        **breadth,
        "high_impact_events_today": len([e for e in high_impact
                                         if datetime.date.today().isoformat() in e.get("date","")]),
        "open_positions": pos_count,
    })
    log.info(f"  Regime: {regime['regime'].upper()} (confidence={regime['confidence']})")
    log.info(f"  Bias: {regime['trade_bias']}")
    log.info(f"  Rationale: {regime['rationale']}")

    # ── 4. VCP screen ─────────────────────────────────────────────────────────
    log.info("── VCP screen")
    vcp_raw = screen()
    top_vcps = vcp_raw[:15]   # send top 15 to Claude

    if top_vcps:
        log.info(f"── Claude: scoring {len(top_vcps)} VCP candidates")
        try:
            scored = score_vcp_candidates(top_vcps)
        except Exception as e:
            log.warning(f"Claude VCP scoring failed: {e} — using raw screener scores")
            scored = [{**s, "score": s.get("raw_score", s.get("score", 0)),
                       "action": "BUY" if s.get("raw_score", s.get("score", 0)) >= 50
                                 else "WATCH",
                       "reason": f"fallback (Claude unavailable): raw={s.get('raw_score', s.get('score', 0))}"}
                      for s in sorted(top_vcps, key=lambda x: x.get("raw_score", x.get("score", 0)), reverse=True)]

        buy_list = [s for s in scored if s.get("action") == "BUY"]
        log.info(f"  BUY candidates: {len(buy_list)}")

        for s in buy_list[:5]:
            log.info(f"  ★ {s['symbol']:6} score={s['score']:3} | {s['reason']}")

        # Save watchlist for market-open routine
        watchlist_path = os.path.join(config.STATE_DIR, "pre_market_watchlist.json")
        with open(watchlist_path, "w") as f:
            json.dump({
                "regime":    regime,
                "buy_list":  buy_list,
                "generated": datetime.datetime.now(ET).isoformat(),
            }, f, indent=2)
        log.info(f"  Watchlist saved → {watchlist_path}")
    else:
        log.info("  No VCP candidates found")

    # ── 5. News headlines ─────────────────────────────────────────────────────
    log.info("── Market news (top 5)")
    news = get_news(limit=5)
    for n in news[:5]:
        log.info(f"  📰 [{n.get('symbol','')}] {n.get('title','')[:80]}")

    # ── 6. Day plan summary ───────────────────────────────────────────────────
    log.info("── Day plan")
    if regime["trade_bias"] == "cash":
        log.info("  ⚠️  CASH BIAS — no new entries today")
    elif regime["trade_bias"] == "defensive":
        log.info("  🛡️  DEFENSIVE — tight stops, reduced size")
    elif regime["trade_bias"] == "aggressive":
        log.info("  🚀  AGGRESSIVE — full size, high-confidence setups only")
    else:
        log.info("  ⚖️  MODERATE — standard size, quality setups")

    slots_available = config.MAX_OPEN_POSITIONS - pos_count
    log.info(f"  Position slots available: {slots_available}")
    log.info(f"  Max deploy per trade: ${pv * config.MAX_POSITION_SIZE_PCT:,.0f}")

    send_premarket_brief(
        date=datetime.date.today().isoformat(),
        regime=regime["regime"],
        bias=regime["trade_bias"],
        rationale=regime.get("rationale", ""),
        portfolio_value=pv,
        cash=cash,
        slots=slots_available,
        buy_list=buy_list if top_vcps else [],
        high_impact_events=high_impact,
    )

    logger.banner(log, "PRE-MARKET COMPLETE")


if __name__ == "__main__":
    run()
