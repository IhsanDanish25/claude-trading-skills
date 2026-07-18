"""
WEEKLY CSP ROUTINE — 9:45 AM ET, Monday
Generates weekly Cash-Secured Put picks and saves to weekly_csp_order.json.
Execute on Monday when deposit clears.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import datetime
import pytz

from core import logger, config
from core.broker import BrokerClient
from core.csp_screener import screen_csp_candidates, pick_best

log = logger.setup("weekly_csp")
ET  = pytz.timezone("America/New_York")

STATE_FILE = os.path.join(config.STATE_DIR, "weekly_csp_order.json")


def run():
    config.validate()
    logger.banner(log, "WEEKLY CSP — Monday 9:45 AM ET")

    broker = BrokerClient()
    acct   = broker.get_account()
    pv     = float(acct.portfolio_value)
    cash   = float(acct.cash)

    log.info(f"Portfolio: ${pv:,.2f} | Cash: ${cash:,.2f}")

    # ── Market regime check ─────────────────────────────────────────────────
    try:
        from core.fmp import get_market_breadth
        breadth = get_market_breadth()
        spy_chg = breadth.get("spy_change_pct", 0)
        regimes = {
            spy_chg >= 0.3: "BULLISH",
            spy_chg >= 0:   "NEUTRAL",
            spy_chg >= -0.5: "DEFENSIVE",
            True:           "AVOID_CSP",
        }
        regime = next(v for k, v in regimes.items() if k)
        log.info(f"  Regime: {regime} | SPY: {spy_chg:+.2f}%")
    except Exception as e:
        log.warning(f"Breadth check failed: {e} — assuming NEUTRAL")
        regime = "NEUTRAL"

    if regime == "AVOID_CSP":
        log.warning("  ⚠️  AVOIDING CSP — market too weak. Cash only.")
        _save_skip(pv, regime, "market_too_weak")
        return

    # ── Screen candidates ──────────────────────────────────────────────────
    candidates = screen_csp_candidates(broker, min_premium=10)
    log.info(f"  Candidates: {len(candidates)}")

    best = pick_best(candidates)
    if not best:
        log.info("  No actionable CSP this week")
        _save_skip(pv, regime, "no_candidates")
        return

    # ── Build order ────────────────────────────────────────────────────────
    order = {
        "generated": datetime.datetime.now(ET).isoformat(),
        "strategy": "WEEKLY_CSP",
        "account": acct.account_number,
        "portfolio_value": pv,
        "cash_available": cash,
        "regime": regime,
        "pick": best,
        "candidates": [c for c in candidates if c.get("type") == "csp"],
    }

    # ── Execution logic ─────────────────────────────────────────────────────
    if regime in ("BULLISH", "NEUTRAL"):

        log.info(f"  ★ TOP PICK: {best['symbol']} ${best.get('strike', 'N/A')} CSP")

        collateral_ratio = best.get("collateral", 0) / pv
        if collateral_ratio > 0.40:
            log.warning(f"  ⚠️  Collateral {collateral_ratio:.0%} exceeds 40% — reduce qty or skip")
            order["status"] = "REVIEW_NEEDED"
        else:
            order["status"] = "READY_TO_EXECUTE"

        log.info(f"  Premium: ~${best.get('premium', 0):.2f}")
        log.info(f"  Win rate: {best.get('win_rate', 0)}%")
        log.info(f"  Collateral: ${best.get('collateral', 0):.2f}")

    # ── Save order ──────────────────────────────────────────────────────────
    with open(STATE_FILE, "w") as f:
        json.dump(order, f, indent=2)

    log.info(f"  Saved → {STATE_FILE}")
    log.info(f"  Status: {order['status']}")


def _save_skip(pv, regime, reason):
    order = {
        "generated": datetime.datetime.now(ET).isoformat(),
        "strategy": "WEEKLY_CSP_SKIPPED",
        "account": config.ALPACA_API_KEY[-8:],
        "portfolio_value": pv,
        "regime": regime,
        "reason": reason,
        "status": "SKIPPED",
    }
    with open(STATE_FILE, "w") as f:
        json.dump(order, f, indent=2)
    log.info(f"  Saved skip → {STATE_FILE}")


if __name__ == "__main__":
    run()