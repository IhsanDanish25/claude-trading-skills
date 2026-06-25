from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
import pytz
import time

from core import logger, config
from core.broker import BrokerClient
from core.fmp import get_quotes
from core.screener import screen
from core.notifier import send_trade_alert

log = logger.setup("market_open")
ET  = pytz.timezone("America/New_York")


def run():
    config.validate()
    logger.banner(log, "MARKET OPEN — 9:30 AM ET")

    broker = BrokerClient()

    # Wait for market open
    for attempt in range(30):
        if broker.is_market_open():
            log.info("Market is OPEN ✓")
            break
        time.sleep(10)
    else:
        log.error("Market closed — aborting")
        return

    # 5min settle
    now_et = datetime.datetime.now(ET)
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    wait_until = open_t + datetime.timedelta(minutes=5)
    if now_et < wait_until:
        secs = (wait_until - now_et).total_seconds()
        log.info(f"Settling {secs:.0f}s...")
        time.sleep(secs)

    pv        = broker.portfolio_value()
    pos_count = broker.position_count()
    slots     = min(3, config.MAX_OPEN_POSITIONS - pos_count)

    log.info(f"Portfolio: ${pv:,.2f} | Positions: {pos_count} | Slots: {slots}")

    if slots <= 0:
        log.info("No slots — done")
        return

    # Screen
    log.info("Screening...")
    candidates = screen()
    log.info(f"Got {len(candidates)} candidates")

    if not candidates:
        log.info("No candidates — done")
        return

    # Live quotes
    symbols = [c["symbol"] for c in candidates[:20]]
    quotes  = get_quotes(symbols)

    # Filter: price 5-500, volume > 5000
    confirmed = []
    for c in candidates[:20]:
        sym = c["symbol"]
        q   = quotes.get(sym, {})
        if not q:
            continue
        price = float(q.get("price", 0))
        vol   = float(q.get("volume", 0))
        if 5.0 <= price <= 500.0 and vol > 5000:
            confirmed.append({**c, "live_price": price})
            log.info(f"  ✓ {sym} ${price:.2f} vol={vol:,.0f}")
        else:
            log.info(f"  ✗ {sym} ${price:.2f} vol={vol:,.0f} — skip")

    log.info(f"Confirmed: {len(confirmed)}")

    if not confirmed:
        log.info("Nothing passed — done")
        return

    # BUY TOP 3
    for c in confirmed[:slots]:
        sym    = c["symbol"]
        amount = pv * config.MAX_POSITION_SIZE_PCT

        log.info(f"BUYING {sym} ${amount:,.0f}")
        try:
            result = broker.buy(sym, dollar_amount=amount)
            log.info(f"✓ {sym} {result['qty']} shares @ ${result['price']:.2f} SL={result['stop']} TP={result['target']}")
            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=result["target"],
                reason="VCP setup",
            )
        except Exception as e:
            log.error(f"✗ {sym} failed: {e}")

    logger.banner(log, "MARKET OPEN COMPLETE")


if __name__ == "__main__":
    run()
