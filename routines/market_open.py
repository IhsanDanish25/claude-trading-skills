from __future__ import annotations
"""
MARKET-OPEN ROUTINE — 8:30 AM ET, Mon-Fri
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
from core.broker       import BrokerClient
from core.auto_trader  import AutoTrader
from core.fmp          import get_quotes, get_market_breadth
from core.analyst      import analyze_market_regime, score_vcp_candidates
from core.screener     import screen

log = logger.setup("market_open")
ET  = pytz.timezone("America/New_York")

WATCHLIST_PATH = "/tmp/pre_market_watchlist.json"
MAX_BUYS       = 3   # max new entries at open


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
    logger.banner(log, "MARKET OPEN — 8:30 AM ET")

    broker = BrokerClient()

    # ── Wait for market open (up to 5 min — routine now fires at 9:30 ET) ───
    for attempt in range(30):  # wait up to 5 min
        if broker.is_market_open():
            log.info("Market is OPEN ✓")
            break
        if attempt % 6 == 0:
            log.info(f"Market not yet open, waiting... ({attempt * 10 // 60} min elapsed)")
        time.sleep(10)
    else:
        log.error("Market still closed after 5 min wait — aborting")
        return

    # ── Load watchlist ────────────────────────────────────────────────────────
    watchlist_data = load_watchlist()

    if watchlist_data:
        regime   = watchlist_data.get("regime", {})
        buy_list = watchlist_data.get("buy_list", [])
    else:
        # Fresh screen + regime
        breadth = get_market_breadth()
        regime  = analyze_market_regime(breadth)
        raw     = screen()
        buy_list = score_vcp_candidates(raw[:15])
        buy_list = [s for s in buy_list if s.get("action") == "BUY"]

    trade_bias = regime.get("trade_bias", "moderate")
    log.info(f"Regime: {regime.get('regime','?').upper()} | Bias: {trade_bias}")

    if trade_bias == "cash":
        log.warning("Cash bias — NO new entries")
        return

    # ── Account check ─────────────────────────────────────────────────────────
    pv        = broker.portfolio_value()
    pos_count = broker.position_count()
    slots     = config.MAX_OPEN_POSITIONS - pos_count

    log.info(f"Portfolio: ${pv:,.2f} | Positions: {pos_count} | Slots: {slots}")

    if slots <= 0:
        log.info("No slots available — no buys")
        return

    if not buy_list:
        log.info("No BUY candidates — nothing to execute")
        return

    # ── Live quote filter: confirm volume + price ─────────────────────────────
    symbols     = [c["symbol"] for c in buy_list[:10]]
    live_quotes = get_quotes(symbols)

    confirmed = []
    for candidate in buy_list:
        sym = candidate["symbol"]
        q   = live_quotes.get(sym, {})
        if not q:
            continue

        live_price  = float(q.get("price", 0))
        live_vol    = float(q.get("volume", 0))
        avg_vol     = float(q.get("avgVolume", 1))
        live_rel_v  = live_vol / avg_vol if avg_vol > 0 else 0

        # At open: rel volume will be low — normalize for time-of-day
        # 8:30 AM = ~10 min in. Expect ~(10/390) of daily vol ≈ 2.5%
        # So actual rel vol check loosened
        passes_vol   = live_rel_v >= 0.3 or live_vol > 100_000
        passes_price = config.MIN_PRICE <= live_price <= config.MAX_PRICE

        log.info(f"  {sym}: ${live_price:.2f} | rel_vol={live_rel_v:.2f} | "
                 f"vol={passes_vol} price={passes_price}")

        if passes_vol and passes_price:
            confirmed.append({**candidate, "live_price": live_price, "live_rel_vol": live_rel_v})

    log.info(f"Confirmed after live filter: {len(confirmed)}")

    # ── Execute buys via AutoTrader ──────────────────────────────────────────
    if trade_bias == "defensive":
        max_buys = min(1, min(MAX_BUYS, slots))
        size_factor = 0.5
    elif trade_bias == "aggressive":
        max_buys = min(MAX_BUYS, slots)
        size_factor = 1.0
    else:
        max_buys = min(MAX_BUYS, slots)
        size_factor = 0.75

    trader  = AutoTrader(broker)
    results = trader.execute_signals(
        confirmed, max_buys=max_buys, size_factor=size_factor,
    )

    buys_taken = sum(1 for r in results if r.success)
    log.info(f"Market open complete | Buys taken: {buys_taken}")
    logger.banner(log, "MARKET OPEN COMPLETE")


if __name__ == "__main__":
    run()
