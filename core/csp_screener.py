"""
CSP SCREENER — Weekly Cash-Secured Put Generator
Scans for optimal premium plays based on support + IV + days-to-expiry.
"""
import os, sys, json
from datetime import datetime, timedelta
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import logger, config
from core.broker import BrokerClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data import StockDataClient

log = logger.setup("csp_screener")
ET = pytz.timezone("America/New_York")


def get_iv_and_strikes(symbol: str, ref_price: float) -> dict:
    """Stub — returns mock premium estimates.
    Replace with real options API call when available."""
    # Simplified model: IV 30-50%, premium ≈ ref_price × 0.03 × DTE/7
    import random
    iv = random.uniform(0.30, 0.55)
    dte = 7
    premium_pct = iv * (dte / 365) * ref_price
    return {
        "symbol": symbol,
        "ref_price": ref_price,
        "iv": round(iv * 100, 1),
        "strike_pct": 0.95,
        "strike": round(ref_price * 0.95, 2),
        "premium": round(premium_pct, 2),
        "premium_pct": round(premium_pct / (ref_price * 0.95) * 100, 2),
        "dte": dte,
        "win_rate": round(random.uniform(70, 82), 1),
    }


def screen_csp_candidates(broker: BrokerClient, min_premium: float = 12) -> list:
    """
    Scan held positions + hot sectors for CSP plays.
    Candidates: oversold stocks near support, OR high-quality names
    the account already wants to own.
    """
    candidates = []

    # 1. Held positions — sell covered puts if holding shares
    for pos in broker.get_positions():
        try:
            price = float(pos.market_value) / float(pos.qty)
            result = {
                "symbol": pos.symbol,
                "ref_price": round(price, 2),
                "type": "covered_csp",
                "action": "ROLL or CLOSE",
                "note": f"Holding {pos.qty} shares at avg ${pos.avg_entry_price}"
            }
            candidates.append(result)
        except Exception:
            pass

    # 2. Market oversold picks — these are the live CSP candidates
    # Replace hardcoded list with VCP / mean-rev screen going forward
    WATCH_LIST = [
        ("INTC", 95.0, 90.0),   # support: $90
        ("NOK", 10.10, 9.50),   # support: $9.50
        ("AMD", 495.0, 480.0),  # support: $480
    ]

    # Filter only if candidate premium is worth it
    MAX_COLLATERAL_PCT = 0.15  # max $39 for $266 account

    for symbol, ref, support in WATCH_LIST:
        strike = round(support, 2)
        collateral = strike * 100
        if collateral / config.equity() > MAX_COLLATERAL_PCT:
            continue

        iv_model = get_iv_and_strikes(symbol, ref)
        if iv_model["premium"] < min_premium:
            log.info(f"  SKIP {symbol} — premium ${iv_model['premium']:.2f} below ${min_premium}")
            continue

        candidates.append({
            "symbol": symbol,
            "ref_price": ref,
            "support": support,
            "strike": strike,
            "premium": iv_model["premium"],
            "premium_pct": iv_model["premium_pct"],
            "dte": 7,
            "win_rate": iv_model["win_rate"],
            "collateral": collateral,
            "type": "csp",
            "action": "SELL",
            "reason": f"Oversold at {ref}, support at {support}",
            "assignment_value": collateral,
        })

    # Sort by premium (highest first)
    candidates.sort(key=lambda x: x.get("premium", 0), reverse=True)
    return candidates


def pick_best(candidates: list) -> dict | None:
    """Return the top CSP candidate that's actionable."""
    for c in candidates:
        if c.get("type") == "csp" and c.get("action") == "SELL":
            return c
    return None


def run():
    config.validate()
    logger.banner(log, "CSP SCREENER — Weekly Pick")

    broker = BrokerClient()
    cash = float(broker.get_account().cash)

    log.info(f"Cash available: ${cash:.2f}")

    candidates = screen_csp_candidates(broker, min_premium=10)

    log.info(f"Candidates found: {len(candidates)}")
    for c in candidates:
        if c["type"] == "csp":
            log.info(f"  ★ {c['symbol']:6} strike=${c['strike']} "
                     f"premium=${c['premium']:.2f} "
                     f"win_rate={c['win_rate']}%")

    best = pick_best(candidates)
    if best:
        log.info(f"\n  TOP PICK: {best['symbol']} ${best['strike']} CSP")
        log.info(f"  Premium:   ${best['premium']:.2f}")
        log.info(f"  Collateral: ${best['collateral']:.2f}")
        log.info(f"  Win rate:  {best['win_rate']}%")
        return best
    else:
        log.info("  No actionable CSP this week")
        return None


if __name__ == "__main__":
    run()