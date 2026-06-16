#!/opt/homebrew/bin/python3.11
"""
Midday Routine — 12:00 EST / 15:00 Riyadh
Risk mitigation + portfolio rebalance
READ memory → REVIEW → WRITE memory
"""
import os, logging
from pathlib import Path
from datetime import datetime
import anthropic
import alpaca_trade_api as tradeapi

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_URL    = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
MEMORY_DIR    = Path(__file__).parent.parent / "memory"
DRY_RUN       = os.environ.get("AUTO_TRADER_DRY_RUN", "0") == "1"
HARD_STOP_PCT = -0.07

def read_memory():
    return {f.stem: f.read_text(encoding="utf-8") for f in MEMORY_DIR.glob("*.md")}

def write_memory(filename, content):
    (MEMORY_DIR / filename).write_text(content, encoding="utf-8")

def run():
    logger.info("=== MIDDAY ROUTINE | %s ===", datetime.now().strftime("%Y-%m-%d %H:%M"))

    memory = read_memory()
    api = tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, ALPACA_URL)

    try:
        positions = api.list_positions()
    except Exception as e:
        logger.error("Failed to get positions: %s", e); return

    if not positions:
        logger.info("No positions to review"); return

    exits = []
    for pos in positions:
        pnl_pct = float(pos.unrealized_plpc)
        symbol = pos.symbol
        logger.info("%s: P&L %.2f%%", symbol, pnl_pct * 100)

        if pnl_pct <= HARD_STOP_PCT:
            logger.warning("%s: HIT HARD STOP (%.2f%%) — EXITING", symbol, pnl_pct * 100)
            if not DRY_RUN:
                try:
                    api.submit_order(symbol=symbol, qty=pos.qty, side="sell",
                                    type="market", time_in_force="day")
                    logger.info("✅ EXITED %s", symbol)
                    exits.append(symbol)
                except Exception as e:
                    logger.error("Exit failed for %s: %s", symbol, e)
            else:
                logger.info("[DRY-RUN] would EXIT %s", symbol)
                exits.append(symbol)

    # Update research log with midday notes
    research = memory.get("research_log", "")
    midday_note = f"\n\n## Midday Review {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    midday_note += f"- Positions reviewed: {len(positions)}\n"
    midday_note += f"- Exits triggered: {exits if exits else 'None'}\n"
    write_memory("research_log.md", research + midday_note)

    logger.info("=== MIDDAY COMPLETE | %d exits ===", len(exits))

if __name__ == "__main__":
    run()
