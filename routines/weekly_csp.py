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
        # NOTE: these branches used to be a dict literal keyed on the boolean
        # expressions themselves. For any spy_chg, every threshold from
        # `>= 0.3` down to `>= -0.5` that holds evaluates to the same key
        # (True), so later entries silently overwrote earlier ones and the
        # dict always collapsed to just {True: "AVOID_CSP"} (or, when no
        # threshold held, {False: "DEFENSIVE", True: "AVOID_CSP"}) — regime
        # was AVOID_CSP on every single run regardless of spy_chg, so CSP
        # execution never fired. An if/elif chain doesn't have this problem.
        if spy_chg >= 0.3:
            regime = "BULLISH"
        elif spy_chg >= 0:
            regime = "NEUTRAL"
        elif spy_chg >= -0.5:
            regime = "DEFENSIVE"
        else:
            regime = "AVOID_CSP"
        log.info(f"  Regime: {regime} | SPY: {spy_chg:+.2f}%")
    except Exception as e:
        log.warning(f"Breadth check failed: {e} — assuming NEUTRAL")
        regime = "NEUTRAL"

    if regime == "AVOID_CSP":
        log.warning("  ⚠️  AVOIDING CSP — market too weak. Cash only.")
        _save_skip(pv, regime, "market_too_weak")
        return

    # ── Screen candidates ──────────────────────────────────────────────────
    # screen_csp_candidates's real parameter is min_premium_pct (a percent-of-
    # collateral weekly-return threshold, default 0.30%) — the previous
    # `min_premium=10` call used a kwarg that doesn't exist on the function at
    # all (TypeError on every run) and, even fixed to the right name, `10`
    # would mean "require a 10% weekly return," which no realistic weekly CSP
    # clears. Use the screener's own documented default instead of guessing
    # a number for a unit this call was never actually written against.
    candidates = screen_csp_candidates(broker)
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
        log.info(f"  RSI: {best.get('rsi', '?')} | MeanRev score: {best.get('meanrev_score', '?')}")
        log.info(f"  Premium: ~${best.get('premium', 0):.2f} ({best.get('premium_pct', 0):.2f}%/week)")
        log.info(f"  Collateral: ${best.get('collateral', 0):.2f} | DTE: {best.get('dte', 7)}")

        collateral_ratio = best.get("collateral", 0) / pv
        if collateral_ratio > 0.85:
            log.warning(f"  ⚠️  Collateral {collateral_ratio:.0%} exceeds 85% of portfolio — skipping")
            order["status"] = "REVIEW_NEEDED"
        else:
            order["status"] = "READY_TO_EXECUTE"
            # Auto-execute: place the CSP order via Alpaca
            opt_level = broker.options_level()
            log.info(f"  Options level: {opt_level}")
            if opt_level < 1:
                log.error("  OPTIONS NOT APPROVED — skipping execution")
                order["status"] = "OPTIONS_NOT_APPROVED"
            elif cash < best.get("collateral", 0):
                log.warning("  Insufficient cash ($%.2f) for collateral ($%.2f)",
                            cash, best.get("collateral", 0))
                order["status"] = "INSUFFICIENT_CASH"
            else:
                try:
                    result = broker.sell_csp(
                        symbol=best["symbol"],
                        strike=best["strike"],
                        expiration=best["expiration"],
                        premium=best.get("premium_per_share"),
                        qty=1,
                    )
                    if result.get("blocked"):
                        log.warning("  CSP blocked: %s", result.get("reason"))
                        order["status"] = "BLOCKED"
                        order["block_reason"] = result.get("reason")
                    else:
                        order["status"] = "EXECUTED"
                        order["execution"] = {
                            k: v for k, v in result.items() if k != "order"
                        }
                        premium_collected = result.get("premium_collected", 0)
                        log.info("  ✅ CSP EXECUTED: %s $%.2f put exp %s | premium=$%.2f",
                                 best["symbol"], best["strike"],
                                 best["expiration"], premium_collected)
                        try:
                            from core.notifier import send_trade_alert
                            send_trade_alert(
                                action="SELL",
                                ticker=best["symbol"],
                                shares=1,
                                price=best["strike"],
                                stop=0,
                                target=0,
                                reason=(f"CSP sell-to-open: ${best['strike']} put exp "
                                        f"{best['expiration']} | premium=${premium_collected:.2f} | "
                                        f"RSI={best.get('rsi', '?')}"),
                            )
                        except Exception as ne:
                            log.warning("Notify failed: %s", ne)
                except Exception as e:
                    log.error("  CSP execution error: %s", e)
                    order["status"] = "EXECUTION_FAILED"
                    order["error"] = str(e)

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