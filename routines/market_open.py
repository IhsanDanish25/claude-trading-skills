#!/opt/homebrew/bin/python3.11
"""
Market Open Routine — 08:30 EST / 11:30 Riyadh
Execute planned trades from pre-market research
READ memory → TRADE → WRITE memory
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
DRY_RUN       = os.environ.get("AUTO_TRADER_DRY_RUN", "0") == "1"

def read_memory():
    memory = {}
    for f in MEMORY_DIR.glob("*.md"):
        memory[f.stem] = f.read_text(encoding="utf-8")
    return memory

def write_memory(filename, content):
    (MEMORY_DIR / filename).write_text(content, encoding="utf-8")
    logger.info("Memory updated: %s", filename)

def run():
    logger.info("=== MARKET OPEN ROUTINE | %s ===", datetime.now().strftime("%Y-%m-%d %H:%M"))

    # READ
    memory = read_memory()
    api = tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, ALPACA_URL)
    acct = api.get_account()
    cash = float(acct.cash)
    equity = float(acct.equity)
    logger.info("Portfolio | Cash: $%.2f | Equity: $%.2f", cash, equity)

    if not api.get_clock().is_open:
        logger.info("Market not open yet"); return

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # Get positions
    try:
        positions = api.list_positions()
        pos_summary = "\n".join([f"- {p.symbol}: {p.qty} shares @ ${p.avg_entry_price} | P&L: ${p.unrealized_pl}" for p in positions])
    except:
        pos_summary = "No positions"

    prompt = f"""You are an autonomous trading agent executing the market open routine.

AGENT INSTRUCTIONS:
{memory.get('agent_instructions', '')}

TRADING STRATEGY:
{memory.get('trading_strategy', '')}

PRE-MARKET RESEARCH (from earlier today):
{memory.get('research_log', '')}

TRADE LOG:
{memory.get('trade_log', '')}

CURRENT PORTFOLIO:
- Cash: ${cash:,.2f}
- Equity: ${equity:,.2f}
- Current Positions: {pos_summary}

TASK: Market open execution decision.
1. Based on pre-market research, which trades should execute NOW?
2. For each trade: ticker, shares, entry reason, stop price
3. Any positions to exit based on new information?

Return JSON:
{{
  "trades_to_execute": [
    {{"ticker": "NVDA", "action": "BUY", "reason": "VCP breakout + defense theme", "confidence": 8}}
  ],
  "positions_to_exit": [],
  "market_assessment": "BULLISH/NEUTRAL/BEARISH",
  "notes": "brief summary"
}}"""

    try:
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        result = json.loads(text[text.find("{"):text.rfind("}")+1])
        logger.info("Opus 4.7 decision: %s", result)

        trades_executed = []
        for trade in result.get("trades_to_execute", []):
            ticker = trade.get("ticker")
            confidence = trade.get("confidence", 0)

            if confidence < 7:
                logger.info("%s: confidence too low (%s/10)", ticker, confidence); continue

            try:
                api.get_position(ticker)
                logger.info("%s: already holding", ticker); continue
            except: pass

            try:
                price = float(api.get_latest_trade(ticker).price)
                stop = round(price * 0.93, 2)
                risk_per_share = price - stop
                shares = int((cash * 0.01) / risk_per_share)
                shares = min(shares, int(cash * 0.25 / price))
                if shares < 1: continue

                if DRY_RUN:
                    logger.info("[DRY-RUN] BUY %d %s @ $%.2f | Stop $%.2f", shares, ticker, price, stop)
                    trades_executed.append(f"{ticker} {shares}sh @${price:.2f}")
                else:
                    order = api.submit_order(symbol=ticker, qty=shares, side="buy",
                                            type="market", time_in_force="day")
                    api.submit_order(symbol=ticker, qty=shares, side="sell",
                                    type="trailing_stop", trail_percent=10,
                                    time_in_force="gtc")
                    logger.info("✅ BUY %d %s @ ~$%.2f | 10%% trailing stop | ID: %s",
                               shares, ticker, price, order.id)
                    trades_executed.append(f"{ticker} {shares}sh @${price:.2f}")
            except Exception as e:
                logger.error("%s trade failed: %s", ticker, e)

        # WRITE — update trade log
        today = date.today().isoformat()
        trade_log = memory.get("trade_log", "")
        if trades_executed:
            new_entries = "\n".join([f"| {today} | {t} | OPEN | - | - | - | Market open routine |"
                                    for t in trades_executed])
            trade_log = trade_log.replace("_None_", new_entries)
            write_memory("trade_log.md", trade_log)

        logger.info("=== MARKET OPEN COMPLETE | %d trades ===", len(trades_executed))

    except Exception as e:
        logger.error("Market open routine failed: %s", e)

if __name__ == "__main__":
    run()
