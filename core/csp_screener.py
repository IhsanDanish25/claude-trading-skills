"""
CSP SCREENER — Weekly Cash-Secured Put Generator (v2)

Pipeline:
  1. Run meanrev screener → oversold stocks (RSI<30, below lower BB)
  2. For each candidate, fetch real Alpaca put option chain
  3. Find best 5% OTM strike expiring next Friday within budget
  4. Sort by premium_pct (highest weekly income first)

Why meanrev → CSP?
  Oversold stocks have elevated IV → fatter premiums.
  If assigned, you buy at a discount (meanrev thesis still holds).
  Win rate is high: stock only needs to stay above an already-oversold strike.
"""
from __future__ import annotations

import datetime
import logging
import math
import os
import sys

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config

log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

_OTM_PCT = 0.95            # target strike 5% below current price
_MAX_COLLATERAL_PCT = 0.85  # never tie up more than 85% of cash in one contract
_MIN_PREMIUM_PCT = 0.30     # minimum 0.30% weekly return on collateral
_IV_ESTIMATE = 0.40         # fallback IV when no live quote available


def _next_friday() -> str:
    today = datetime.date.today()
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (today + datetime.timedelta(days=days_ahead)).isoformat()


def _estimate_premium(strike: float, dte: int, iv: float = _IV_ESTIMATE) -> float:
    """Black-Scholes approximation for ATM put premium. Rough but directionally correct."""
    return round(strike * iv * math.sqrt(dte / 365) * 0.4, 2)


def _contract_premium(contract) -> float:
    """Extract per-share premium from an Alpaca option contract object."""
    for attr in ("close_price", "last_price", "ask_price", "bid_price"):
        val = float(getattr(contract, attr, 0) or 0)
        if val > 0:
            return val
    return 0.0


def screen_csp_candidates(broker, min_premium_pct: float = _MIN_PREMIUM_PCT) -> list:
    """
    Return CSP candidates sorted by weekly premium_pct (highest first).
    Each dict has: symbol, ref_price, rsi, strike, expiration, dte,
                   premium, premium_pct, collateral, contract_symbol.
    """
    acct = broker.get_account()
    cash = float(acct.cash)
    options_bp = float(getattr(acct, "options_buying_power", cash) or cash)
    max_collateral = min(cash, options_bp) * _MAX_COLLATERAL_PCT

    log.info("CSP budget: cash=$%.2f options_bp=$%.2f max_collateral=$%.2f",
             cash, options_bp, max_collateral)

    if max_collateral < 50:
        log.warning("Insufficient budget for CSP (need ≥$50 collateral, have $%.2f)", max_collateral)
        return []

    # Step 1 — oversold candidates from meanrev screener
    raw_candidates: list[dict] = []
    try:
        from core.meanrev_screener import screen as _meanrev
        raw_candidates = _meanrev()
        log.info("MeanRev: %d oversold candidates", len(raw_candidates))
    except Exception as e:
        log.error("MeanRev screener failed: %s — using fallback watchlist", e)

    # Fallback: cheap liquid names from SP80 universe when meanrev finds nothing
    if not raw_candidates:
        fallback_syms = [s for s in getattr(config, "SP80_UNIVERSE", [])
                         if s in ("F", "INTC", "NOK", "SNAP", "SOFI", "PLTR",
                                  "BAC", "WFC", "C", "T", "VZ", "CSCO")]
        raw_candidates = [{"symbol": s, "price": 0, "rsi": 35, "score": 50,
                            "bb_position": 0} for s in fallback_syms[:10]]
        log.info("Fallback watchlist: %s", [c["symbol"] for c in raw_candidates])

    expiration = _next_friday()
    results: list[dict] = []

    for c in raw_candidates[:15]:
        symbol = c.get("symbol", "")
        if not symbol:
            continue

        ref_price = float(c.get("price") or 0)
        if ref_price <= 0:
            try:
                ref_price = broker.get_price(symbol)
            except Exception:
                log.info("  SKIP %s — cannot fetch price", symbol)
                continue

        if ref_price <= 0:
            continue

        target_strike = round(ref_price * _OTM_PCT, 2)
        est_collateral = target_strike * 100

        if est_collateral > max_collateral:
            log.info("  SKIP %s — collateral $%.0f > budget $%.0f",
                     symbol, est_collateral, max_collateral)
            continue

        # Step 2 — real option chain from Alpaca
        contracts = broker.get_put_contracts(
            symbol, expiration, max_strike=target_strike * 1.10
        )

        if contracts:
            # Pick closest strike at or below target
            best = min(
                contracts,
                key=lambda ct: abs(float(getattr(ct, "strike_price", 0) or 0) - target_strike)
            )
            strike = float(getattr(best, "strike_price", target_strike) or target_strike)
            contract_symbol = getattr(best, "symbol", "")
            premium_per_share = _contract_premium(best)
        else:
            log.info("  %s — no live contracts, using estimated premium", symbol)
            strike = target_strike
            contract_symbol = ""
            premium_per_share = 0.0

        collateral = round(strike * 100, 2)
        if collateral > max_collateral:
            log.info("  SKIP %s — nearest strike $%.2f collateral $%.0f > budget",
                     symbol, strike, collateral)
            continue

        # Fall back to BS estimate when no live quote
        dte = max(1, (datetime.date.fromisoformat(expiration) - datetime.date.today()).days)
        if premium_per_share <= 0:
            premium_per_share = _estimate_premium(strike, dte)
            log.info("  %s — estimated premium $%.2f/share (no live quote)", symbol, premium_per_share)

        premium_contract = round(premium_per_share * 100, 2)
        premium_pct = round(premium_contract / collateral * 100, 3) if collateral > 0 else 0

        if premium_pct < min_premium_pct:
            log.info("  SKIP %s — premium %.3f%% below min %.2f%%",
                     symbol, premium_pct, min_premium_pct)
            continue

        results.append({
            "symbol":           symbol,
            "ref_price":        round(ref_price, 2),
            "rsi":              round(float(c.get("rsi") or 0), 1),
            "bb_position":      round(float(c.get("bb_position") or 0), 1),
            "meanrev_score":    round(float(c.get("score") or 0), 1),
            "strike":           strike,
            "expiration":       expiration,
            "dte":              dte,
            "premium_per_share": premium_per_share,
            "premium":          premium_contract,
            "premium_pct":      premium_pct,
            "collateral":       collateral,
            "contract_symbol":  contract_symbol,
            "type":             "csp",
            "action":           "SELL",
        })

        log.info("  ★ %s strike=$%.2f premium=$%.2f (%.2f%%) collateral=$%.0f DTE=%d",
                 symbol, strike, premium_contract, premium_pct, collateral, dte)

    results.sort(key=lambda x: x["premium_pct"], reverse=True)
    return results


def pick_best(candidates: list) -> dict | None:
    for c in candidates:
        if c.get("type") == "csp" and c.get("action") == "SELL":
            return c
    return None


def run():
    from core import logger
    from core.broker import BrokerClient
    config.validate()
    logger.banner(log, "CSP SCREENER — Weekly Pick")
    broker = BrokerClient()
    candidates = screen_csp_candidates(broker)
    log.info("Candidates: %d", len(candidates))
    best = pick_best(candidates)
    if best:
        log.info("TOP PICK: %s $%.2f put | premium=$%.2f (%.2f%%) | collateral=$%.0f",
                 best["symbol"], best["strike"], best["premium"],
                 best["premium_pct"], best["collateral"])
    else:
        log.info("No actionable CSP this week")
    return best


if __name__ == "__main__":
    run()
