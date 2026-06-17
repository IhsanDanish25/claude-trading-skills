#!/usr/bin/env python3
"""
Pre-Market Routine — 06:00 EST / 09:00 Riyadh
Catalyst identification + deep research via Opus 4.8
READ memory → RESEARCH → WRITE memory
"""
import os, json, logging
from pathlib import Path
from datetime import datetime, date
import anthropic
import alpaca_trade_api as tradeapi

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_URL    = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
MEMORY_DIR    = Path(__file__).parent.parent / "memory"
DASHBOARD_DIR = Path(__file__).parent.parent / "examples/daily-market-dashboard/knowledge"

def read_memory():
    memory = {}
    for f in MEMORY_DIR.glob("*.md"):
        memory[f.stem] = f.read_text(encoding="utf-8")
    return memory

def write_memory(filename, content):
    path = MEMORY_DIR / filename
    path.write_text(content, encoding="utf-8")
    logger.info("Memory updated: %s", filename)

def get_dashboard():
    files = sorted(DASHBOARD_DIR.glob("daily_dashboard_*.md"), reverse=True)
    return files[0].read_text(encoding="utf-8") if files else ""

def run():
    logger.info("=== PRE-MARKET ROUTINE | %s ===", datetime.now().strftime("%Y-%m-%d %H:%M"))

    # READ
    memory = read_memory()
    dashboard = get_dashboard()
    logger.info("Memory loaded: %s", list(memory.keys()))

    # ACT — Opus 4.8 deep research
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""You are an autonomous trading agent. Read your memory and identify today's best opportunities.

AGENT INSTRUCTIONS:
{memory.get('agent_instructions', '')}

TRADING STRATEGY:
{memory.get('trading_strategy', '')}

PREVIOUS RESEARCH LOG:
{memory.get('research_log', '')}

TODAY'S MARKET DASHBOARD:
{dashboard[:2000]}

TASK: Pre-market catalyst identification.
1. Identify 2-3 best fundamental catalysts for today
2. Which VCP candidates align with fundamental thesis?
3. Any macro risks to watch?
4. Rate market conditions: BULLISH / NEUTRAL / BEARISH
5. Update the research log with today's findings

Return updated research_log.md content (full file, markdown format)."""

    try:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        updated_research = response.content[0].text.strip()
        logger.info("Opus 4.8 research complete")

        # WRITE
        write_memory("research_log.md", updated_research)
        logger.info("=== PRE-MARKET COMPLETE ===")

    except Exception as e:
        logger.error("Pre-market research failed: %s", e)

if __name__ == "__main__":
    run()
