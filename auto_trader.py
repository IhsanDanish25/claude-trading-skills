#!/opt/homebrew/bin/python3.11
"""Auto Trader — FTD + VCP + Claude Opus 4.7 + Congress/Buffett insider tracking."""
import os, json, logging, urllib.request, re
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

def get_congress_trades():
    try:
        url = "https://www.capitoltrades.com/api/trades?pageSize=20&page=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        buys = [t for t in data.get("data",[]) if t.get("type","").upper() == "BUY"]
        logger.info("Congress: %d recent buys", len(buys))
        return buys
    except Exception as e:
        logger.warning("Congress trades failed: %s", e)
        return []

def get_buffett_holdings():
    try:
        url = "https://www.dataroma.com/m/holdings.php?m=BRK"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8")
        tickers = re.findall(r'stock=([A-Z]+)"', html)[:10]
        logger.info("Buffett holdings: %s", tickers[:5])
        return tickers
    except Exception as e:
        logger.warning("Buffett holdings failed: %s", e)
        return []

def claude_analyze(claude_client, dashboard, vcp_list, ticker, price, congress_buys, buffett_holdings):
    vcp_info = next((v for v in vcp_list if v.get("symbol","") == ticker), {})
    congress_buying = [t for t in congress_buys if t.get("ticker","").upper() == ticker.upper()]
    buffett_owns = ticker in buffett_holdings

    insider_context = ""
    if congress_buying:
        names = [t.get("politician", t.get("name","Unknown")) for t in congress_buying[:3]]
        insider_context += f"\nCONGRESS BUYING {ticker}: {', '.join(names)}"
    if buffett_owns:
        insider_context += f"\nWARREN BUFFETT holds {ticker} in Berkshire portfolio"
    if not insider_context:
        insider_context = "\nNo notable insider/political buying found"

    prompt = f"""You are a professional momentum trader using Minervini VCP + Munger principles + political insider data.

MARKET DASHBOARD:
{dashboard[:1500]}

VCP SETUP FOR {ticker}:
- Price: ${price:.2f}
- State: {vcp_info.get('execution_state','Unknown')}
- Score: {vcp_info.get('composite_score','N/A')}
- Rating: {vcp_info.get('rating','N/A')}
- Pivot Distance: {vcp_info.get('distance_from_pivot_pct','N/A')}%

POLITICAL & INSIDER DATA:{insider_context}

BUY {ticker} right now? Consider technicals + market + who else is buying.

JSON only:
{{
  "decision": "BUY" or "SKIP",
  "confidence": 1-10,
  "reason": "one sentence",
  "insider_signal": "strong/neutral/weak",
  "risk_note": "one sentence"
}}"""

    try:
        response = claude_client.messages.create(
            model="claude-opus-4-7",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        result = json.loads(text[text.find("{"):text.rfind("}")+1])
        logger.info("Opus 4.7 on %s: %s (%s/10) insider=%s — %s",
                    ticker, result.get("decision"), result.get("confidence"),
                    result.get("insider_signal"), result.get("reason"))
        return result
    except Exception as e:
        logger.error("Claude failed: %s", e)
        return {"decision": "SKIP", "confidence": 0, "reason": "Analysis failed"}

def run():
    logger.info("=== Auto Trader + Opus 4.7 + Insider Tracker | %s ===",
                datetime.now().strftime("%Y-%m-%d %H:%M"))

    api, acct = connect_alpaca()
    claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    cash = float(acct.cash)

    if not api.get_clock().is_open:
        logger.info("Market CLOSED. No trades."); return

    dashboard = load_dashboard()
    if not dashboard:
        logger.error("No dashboard."); return

    ftd = parse_ftd_score(dashboard)
    logger.info("FTD Score: %.1f", ftd)
    if ftd < MIN_FTD_SCORE:
        logger.info("FTD below %d. No trades.", MIN_FTD_SCORE); return

    vcps = load_vcp()
    if not vcps:
        logger.info("No VCP setups. No trades."); return

    # Fetch insider data once
    congress_buys = get_congress_trades()
    buffett_holdings = get_buffett_holdings()

    traded = 0
    for vcp in vcps[:2]:
        ticker = vcp.get("symbol", vcp.get("ticker",""))
        if not ticker: continue

        try:
            api.get_position(ticker)
            logger.info("%s: already holding, skip", ticker); continue
        except: pass

        try:
            price = float(api.get_latest_trade(ticker).price)
        except Exception as e:
            logger.error("%s price failed: %s", ticker, e); continue

        analysis = claude_analyze(claude_client, dashboard, vcps, ticker, price,
                                  congress_buys, buffett_holdings)

        if analysis.get("decision") != "BUY":
            logger.info("%s: SKIP — %s", ticker, analysis.get("reason")); continue

        if analysis.get("confidence", 0) < 7:
            logger.info("%s: confidence %s/10 too low", ticker, analysis.get("confidence")); continue

        stop = price * 0.93
        shares = int((cash * RISK_PCT) / (price - stop))
        shares = min(shares, int(cash * 0.25 / price))
        if shares < 1:
            logger.warning("%s: shares=0, skip", ticker); continue

        try:
            order = api.submit_order(symbol=ticker, qty=shares, side="buy",
                                     type="market", time_in_force="day")
            logger.info("✅ BUY %d %s @ ~$%.2f | Stop $%.2f | Opus:%s/10 | Insider:%s | ID:%s",
                       shares, ticker, price, stop,
                       analysis.get("confidence"), analysis.get("insider_signal"), order.id)
            traded += 1
        except Exception as e:
            logger.error("%s order failed: %s", ticker, e)

    logger.info("=== %d trade(s) placed ===", traded)

if __name__ == "__main__":
    run()
