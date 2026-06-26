#!/usr/bin/env python3
"""Auto Trader — FTD + VCP + Claude Opus 4.7 + Congress/Buffett insider tracking."""
import os, json, logging, urllib.request, re, sys
from pathlib import Path
from datetime import datetime, date
import anthropic
import alpaca_trade_api as tradeapi

# Auto-connect chain: verify Alpaca credentials on startup
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from alpaca_auto_connect import run_chain as alpaca_auto_connect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_URL    = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
DRY_RUN = os.environ.get("AUTO_TRADER_DRY_RUN", "0") == "1"
RISK_PCT      = 0.01
MIN_FTD_SCORE = 80
MAX_POS_PCT   = 0.25
MAX_TRADES_PER_RUN = 2
STOP_LOSS_PCT = 0.07        # 7% trailing-style stop
CONFIDENCE_MIN = 7
DAILY_LOSS_CIRCUIT_PCT = 0.03   # skip trading if equity is down >3% on the day
STATE_FILE = Path(__file__).parent / ".auto_trader_state.json"
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
    # Markdown-table parser per the markdown-table-parser skill:
    # split on '|', strip **/__ bold, lowercase, exact-match the header cell,
    # iterate value cells. Substring match breaks on "AI Quality Score"
    # appearing before "Quality Score" — see skill 5-test suite for proof.
    return _parse_markdown_table_score(content, "quality score")


def _parse_markdown_table_score(content: str, target_header: str) -> float:
    target = target_header.lower()
    for line in content.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 3:
            continue
        header = re.sub(r"\*+", "", cells[1]).strip().lower()
        if header == target:
            for cell in cells[2:]:
                m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*100", cell)
                if m:
                    return float(m.group(1))
    logger.warning(f"Header '{target_header}' not found in dashboard")
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
            model=ANTHROPIC_MODEL,
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

def load_state():
    """Load {date: {ticker: order_id, last_equity: float, day_start_equity: float}} from disk."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:
        logger.warning("State file unreadable, resetting: %s", e)
        return {}

def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning("Could not write state: %s", e)

def daily_pnl_circuit_breaker(api, state):
    """Return (ok, pct_change) — ok=False means we should NOT trade today."""
    today = date.today().isoformat()
    acct = api.get_account()
    equity = float(acct.equity)
    last_equity = float(acct.last_equity)
    # last_equity is end-of-previous-day from Alpaca; today's change is (equity - last_equity) / last_equity
    if last_equity <= 0:
        return True, 0.0
    change = (equity - last_equity) / last_equity
    if change <= -DAILY_LOSS_CIRCUIT_PCT:
        logger.warning("Circuit breaker: equity down %.2f%% today (limit %.2f%%). Halting.",
                       change * 100, DAILY_LOSS_CIRCUIT_PCT * 100)
        return False, change
    return True, change

def attach_stop(api, ticker, qty, stop_price, parent_order_id):
    """Attach a GTC stop-loss to a filled position. Returns order or None."""
    if DRY_RUN:
        logger.info("  [DRY-RUN] would attach stop @ $%.2f for %s", stop_price, ticker)
        return None
    try:
        sl = api.submit_order(
            symbol=ticker,
            qty=qty,
            side="sell",
            type="stop",
            stop_price=round(stop_price, 2),
            time_in_force="gtc",
            client_order_id=f"sl-{parent_order_id}",
        )
        logger.info("  ↳ Stop-loss attached @ $%.2f (ID:%s)", stop_price, sl.id)
        return sl
    except Exception as e:
        logger.error("  ↳ Stop-loss attach FAILED for %s: %s", ticker, e)
        return None

def run():
    logger.info("=== Auto Trader + Opus 4.7 + Insider Tracker | %s ===",
                datetime.now().strftime("%Y-%m-%d %H:%M"))

    # Auto-connect chain: detect env, validate creds, write state
    logger.info("Running Alpaca auto-connect chain...")
    chain_rc = alpaca_auto_connect(dry_run=False, json_output=False)
    if chain_rc != 0:
        logger.error("Alpaca auto-connect failed (exit %d). Aborting.", chain_rc)
        return

    api, acct = connect_alpaca()
    claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    cash = float(acct.cash)

    if not api.get_clock().is_open:
        logger.info("Market CLOSED. No trades."); return

    state = load_state()
    today = date.today().isoformat()
    today_bought = set(state.get(today, {}).get("tickers_bought", []))

    # Daily loss circuit breaker
    ok, change = daily_pnl_circuit_breaker(api, state)
    if not ok:
        logger.info("Daily P&L %.2f%%. Skipping all trades.", change * 100)
        return

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
    for vcp in vcps[:MAX_TRADES_PER_RUN]:
        ticker = vcp.get("symbol", vcp.get("ticker",""))
        if not ticker: continue

        # Skip if we already bought this ticker today
        if ticker in today_bought:
            logger.info("%s: already bought today, skip", ticker); continue

        # Skip if we already hold it
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

        if analysis.get("confidence", 0) < CONFIDENCE_MIN:
            logger.info("%s: confidence %s/10 below %d, skip",
                        ticker, analysis.get("confidence"), CONFIDENCE_MIN); continue

        stop = round(price * (1 - STOP_LOSS_PCT), 2)
        risk_per_share = price - stop
        if risk_per_share <= 0:
            logger.warning("%s: invalid stop calc, skip", ticker); continue
        shares = int((cash * RISK_PCT) / risk_per_share)
        shares = min(shares, int(cash * MAX_POS_PCT / price))
        if shares < 1:
            logger.warning("%s: shares=0, skip", ticker); continue

        if DRY_RUN:
            logger.info("[DRY-RUN] would BUY %d %s @ ~$%.2f | Stop $%.2f | Opus:%s/10 | Insider:%s",
                        shares, ticker, price, stop,
                        analysis.get("confidence"), analysis.get("insider_signal"))
            # Persist state in dry-run too, so re-runs behave consistently
            today_bought.add(ticker)
            state.setdefault(today, {})["tickers_bought"] = sorted(today_bought)
            save_state(state)
            traded += 1
            continue

        try:
            order = api.submit_order(symbol=ticker, qty=shares, side="buy",
                                     type="market", time_in_force="day")
            logger.info("✅ BUY %d %s @ ~$%.2f | Stop $%.2f | Opus:%s/10 | Insider:%s | ID:%s",
                       shares, ticker, price, stop,
                       analysis.get("confidence"), analysis.get("insider_signal"), order.id)

            # Attach GTC stop-loss IMMEDIATELY
            attach_stop(api, ticker, shares, stop, order.id)

            # Persist state so re-runs won't re-buy this ticker today
            today_bought.add(ticker)
            state.setdefault(today, {})["tickers_bought"] = sorted(today_bought)
            save_state(state)

            traded += 1
        except Exception as e:
            logger.error("%s order failed: %s", ticker, e)

    logger.info("=== %d trade(s) placed ===", traded)

if __name__ == "__main__":
    run()
