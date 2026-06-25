from __future__ import annotations
"""
MARKET-OPEN ROUTINE — 9:30 AM ET, Mon-Fri
ALPACA-ONLY. No FMP = no rate limits.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
import pytz
import time

from core import logger, config
from core.broker import BrokerClient
from core.screener import screen
from core.notifier import send_trade_alert

log = logger.setup("market_open")
ET  = pytz.timezone("America/New_York")

MAX_BUYS = 3


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

    pv        = broker.portfolio_value()
    pos_count = broker.position_count()
    slots     = min(MAX_BUYS, config.MAX_OPEN_POSITIONS - pos_count)

    log.info(f"Portfolio: ${pv:,.2f} | Positions: {pos_count} | Slots: {slots}")

    if slots <= 0:
        log.info("No slots — done")
        return

    log.info("Screening...")
    candidates = screen()
    log.info(f"Got {len(candidates)} candidates")

    if not candidates:
        log.info("No candidates — done")
        return

    confirmed = []
    for c in candidates:
        price = c.get("price", 0)
        if 5.0 <= price <= 500.0:
            confirmed.append(c)
            log.info(f"  ✓ {c['symbol']} ${price:.2f} score={c.get('score',0)} relvol={c.get('rel_volume',0)}")

    log.info(f"Confirmed: {len(confirmed)}")

    if not confirmed:
        log.info("Nothing passed price band — done")
        return

    buys_taken = 0
    for c in confirmed[:slots]:
        sym    = c["symbol"]
        amount = pv * config.MAX_POSITION_SIZE_PCT

        log.info(f"BUYING {sym} ${amount:,.0f}")
        try:
            result = broker.buy(sym, dollar_amount=amount)
            log.info(f"✓ {sym} {result['qty']} shares @ ${result['price']:.2f} "
                     f"SL={result['stop']} TP={result['target']}")
            send_trade_alert(
                action="BUY",
                ticker=sym,
                shares=result["qty"],
                price=result["price"],
                stop=result["stop"],
                target=result["target"],
                reason=f"VCP score={c.get('score',0)}",
            )
            buys_taken += 1
        except Exception as e:
            log.error(f"✗ {sym} buy failed: {e}")

    log.info(f"Market open complete | Buys taken: {buys_taken}")
    logger.banner(log, "MARKET OPEN COMPLETE")


if __name__ == "__main__":
    run()
