#!/usr/bin/env python3
"""Auto Trader — FTD + VCP + Claude Opus 4.7 analysis → Alpaca paper trade."""
import os, json, logging
from pathlib import Path
from datetime import datetime
import anthropic
import alpaca_trade_api as tradeapi

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_URL    = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RISK_PCT      = 0.01
MIN_FTD_SCORE = 80
DASHBOARD_DIR = Path(__file__).parent / "examples/daily-market-dashboard/knowledge"

def connect_alpaca():
    api = tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, ALPACA_URL)
    acct = api.get_account()
    logger.info("Alpaca | Cash: $%.2f | %s", float(acct.cash), "PAPER" if "paper" in ALPACA_URL else "LIVE")
    return api, acct

def connect_claude():
    return anthropic.Anthropic(api_key=ANTHROPIC_KEY)

def load_dashboard():
    files = sorted(DASHBOARD_DIR.glob("daily_dashboard_*.md"), reverse=True)
    if not files: return ""
    return files[0].read_text(encoding="utf-8")

def parse_ftd_score(content):
    for line in content.splitlines():
        if "FTD Detector" in line and "|" in line:
            for p in line.split("|"):
                try: return float(p.strip())
                except: pass
    return 0.0

def load_vcp():
    dirs = [Path(__file__).parent, Path(__file__).parent/"examples/daily-market-dashboard"]
    candidates = []
    for d in dirs:
        candidates.extend(d.glob("vcp_screener_*.json"))
    if not candidates: return []
    data = json.loads(max(candidates, key=lambda p: p.stat().st_mtime).read_text())
    return [r for r in data.get("results", []) if isinstance(r, dict)
            and r.get("execution_state","") in ("Pre-breakout","Breakout")]

def claude_analyze(claude, dashboard, vcp_list, ticker, price):
    """Ask Claude Opus 4.7 to confirm or reject the trade."""
    vcp_info = next((v for v in vcp_list if v.get("symbol","") == ticker), {})
    
    prompt = f"""You are a professional momentum trader using Minervini VCP strategy and Munger principles.

MARKET DASHBOARD:
{dashboard[:2000]}

VCP SETUP FOR {ticker}:
- Current Price: ${price:.2f}
- Execution State: {vcp_info.get('execution_state', 'Unknown')}
- Composite Score: {vcp_info.get('composite_score', 'N/A')}
- Rating: {vcp_info.get('rating', 'N/A')}
- Distance from Pivot: {vcp_info.get('distance_from_pivot_pct', 'N/A')}%
- Pattern Type: {vcp_info.get('pattern_type', 'N/A')}

DECISION REQUIRED:
Should I BUY {ticker} right now?

Respond in this exact JSON format only:
{{
  "decision": "BUY" or "SKIP",
  "confidence": 1-10,
  "reason": "one sentence explanation",
  "risk_note": "one sentence about main risk"
}}"""

    try:
        response = claude.messages.create(
            model="claude-opus-4-7",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        # Extract JSON
        start = text.find("{")
        end = text.rfind("}") + 1
        result = json.loads(text[start:end])
        logger.info("Claude on %s: %s (confidence: %s/10) — %s",
                    ticker, result.get("decision"), result.get("confidence"), result.get("reason"))
        return result
    except Exception as e:
        logger.error("Claude analysis failed: %s", e)
        return {"decision": "SKIP", "confidence": 0, "reason": "Claude analysis failed"}

def run():
    logger.info("=== Auto Trader + Claude Opus 4.7 | %s ===", datetime.now().strftime("%Y-%m-%d %H:%M"))

    # Connect
    api, acct = connect_alpaca()
    claude = connect_claude()
    cash = float(acct.cash)

    # Market open?
    if not api.get_clock().is_open:
        logger.info("Market CLOSED. No trades."); return

    # Load dashboard
    dashboard = load_dashboard()
    if not dashboard:
        logger.error("No dashboard found."); return

    # FTD check
    ftd = parse_ftd_score(dashboard)
    logger.info("FTD Score: %.1f", ftd)
    if ftd < MIN_FTD_SCORE:
        logger.info("FTD below %d. No trades.", MIN_FTD_SCORE); return

    # VCP candidates
    vcps = load_vcp()
    if not vcps:
        logger.info("No VCP setups. No trades."); return

    traded = 0
    for vcp in vcps[:2]:
        ticker = vcp.get("symbol", vcp.get("ticker",""))
        if not ticker: continue

        # Skip if holding
        try:
            api.get_position(ticker)
            logger.info("%s: already holding, skip", ticker); continue
        except: pass

        # Get price
        try:
            price = float(api.get_latest_trade(ticker).price)
        except Exception as e:
            logger.error("%s price failed: %s", ticker, e); continue

        # Claude Opus 4.7 analysis
        analysis = claude_analyze(claude, dashboard, vcps, ticker, price)
        
        if analysis.get("decision") != "BUY":
            logger.info("%s: Claude says SKIP — %s", ticker, analysis.get("reason")); continue
        
        if analysis.get("confidence", 0) < 7:
            logger.info("%s: Claude confidence too low (%s/10), skip", 
                       ticker, analysis.get("confidence")); continue

        # Size position
        stop = price * 0.93
        shares = int((cash * RISK_PCT) / (price - stop))
        shares = min(shares, int(cash * 0.25 / price))
        if shares < 1:
            logger.warning("%s: shares=0, skip", ticker); continue

        # Place order
        try:
            order = api.submit_order(
                symbol=ticker, qty=shares, side="buy",
                type="market", time_in_force="day"
            )
            logger.info("✅ BUY %d %s @ ~$%.2f | Stop $%.2f | Claude: %s/10 | ID: %s",
                       shares, ticker, price, stop,
                       analysis.get("confidence"), order.id)
            logger.info("Risk note: %s", analysis.get("risk_note",""))
            traded += 1
        except Exception as e:
            logger.error("%s order failed: %s", ticker, e)

    logger.info("=== %d trade(s) placed ===", traded)

if __name__ == "__main__":
    run()
