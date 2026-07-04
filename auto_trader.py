"""
Auto Trader — FTD + VCP + Claude analysis + Congress/Buffett insider tracking.

Migrated to alpaca-py SDK (replaces deprecated alpaca_trade_api).
All order lifecycle and account calls use the modern TradingClient API.
"""
import os, json, logging, urllib.request, re, sys, time
from pathlib import Path
from datetime import datetime, date
import anthropic

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, ClosePositionRequest,
)
from alpaca.trading.enums import (
    OrderSide, TimeInForce,
)
from alpaca.data.requests import StockLatestTradeRequest

from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, PAPER_TRADE

# Auto-connect chain: verify Alpaca credentials on startup
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from alpaca_auto_connect import run_chain as alpaca_auto_connect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _load_dotenv(path):
    """Load KEY=VALUE pairs from .env, overriding ambient env."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

_load_dotenv(Path(__file__).parent / ".env")

ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
DRY_RUN = os.environ.get("AUTO_TRADER_DRY_RUN", "0") == "1"
RISK_PCT      = 0.01
MIN_FTD_SCORE = 80
MAX_POS_PCT   = 0.25
MAX_TRADES_PER_RUN = 2
STOP_LOSS_PCT = 0.07        # 7% trailing-style stop
LIMIT_BUFFER_PCT = 0.005    # marketable-limit entry: cap slippage at 0.5%
CONFIDENCE_MIN = 7
DAILY_LOSS_CIRCUIT_PCT = 0.03
MAX_DATA_AGE_DAYS = int(os.environ.get("AUTO_TRADER_MAX_DATA_AGE_DAYS", "3"))
FILL_WAIT_SECS = 45
STATE_FILE = Path(__file__).parent / ".auto_trader_state.json"
DASHBOARD_DIR = Path(__file__).parent / "examples/daily-market-dashboard/knowledge"


def connect_alpaca():
    """Build a TradingClient and log account info."""
    client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADE)
    acct = client.get_account()
    mode = "PAPER" if PAPER_TRADE else "LIVE"
    logger.info("Alpaca [%s] | Cash: $%.2f | Equity: $%.2f",
                mode, float(acct.cash), float(acct.equity))
    return client, acct


def verify_anthropic(client):
    """Fail fast on a bad Anthropic key."""
    try:
        client.models.list(limit=1)
        return True
    except anthropic.AuthenticationError:
        logger.error("ANTHROPIC_API_KEY is invalid/revoked. Create a new key at "
                     "console.anthropic.com and update .env. Aborting.")
        return False
    except Exception as e:
        logger.warning("Anthropic pre-flight inconclusive: %s", e)
        return True

def _data_age_days(path):
    """Age of a data file: date embedded in filename if present, else mtime."""
    m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", path.name)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return (date.today() - d).days
        except ValueError:
            pass
    return (date.today() - date.fromtimestamp(path.stat().st_mtime)).days

def load_dashboard():
    files = sorted(DASHBOARD_DIR.glob("daily_dashboard_*.md"), reverse=True)
    if not files:
        return ""
    latest = files[0]
    age = _data_age_days(latest)
    if age > MAX_DATA_AGE_DAYS:
        logger.error("Dashboard %s is %d days old (max %d). Regenerate it before trading.",
                     latest.name, age, MAX_DATA_AGE_DAYS)
        return ""
    return latest.read_text(encoding="utf-8")

def parse_ftd_score(content):
    """FTD quality score, tolerant of both dashboard formats in the wild:
    1. Bullet:     - **Quality Score**: 95
    2. Table row:  header cell 'Quality Score' with 'NN/100' or bare number
    3. Table row:  header cell 'FTD Detector' with bare-number score cell
    """
    m = re.search(r"^\s*-\s*\*\*quality score\*\*\s*:\s*(\d+(?:\.\d+)?)\b",
                  content, re.IGNORECASE | re.MULTILINE)
    if m:
        return float(m.group(1))
    for header in ("quality score", "ftd detector"):
        score = _parse_markdown_table_score(content, header)
        if score > 0:
            return score
    logger.warning("FTD quality score not found in dashboard")
    return 0.0


def _parse_markdown_table_score(content: str, target_header: str) -> float:
    # Markdown-table parser per the markdown-table-parser skill:
    # split on '|', strip **/__ bold, lowercase, exact-match the header cell,
    # iterate value cells. Substring match breaks on "AI Quality Score"
    # appearing before "Quality Score" — see skill 5-test suite for proof.
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
                if re.fullmatch(r"\d+(?:\.\d+)?", cell):
                    return float(cell)
    return 0.0

# Two screener schema generations exist: newer files use execution_state,
# older ones use status. Treat their actionable values as equivalent.
VCP_ACTIONABLE_STATES = ("Pre-breakout", "Breakout", "Setting Up", "Triggered")

def load_vcp():
    dirs = [Path(__file__).parent, Path(__file__).parent/"examples/daily-market-dashboard"]
    candidates = []
    for d in dirs:
        candidates.extend(d.glob("vcp_screener_*.json"))
    if not candidates: return []
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    age = _data_age_days(latest)
    if age > MAX_DATA_AGE_DAYS:
        logger.error("VCP screener %s is %d days old (max %d). Regenerate it before trading.",
                     latest.name, age, MAX_DATA_AGE_DAYS)
        return []
    data = json.loads(latest.read_text())
    return [r for r in data.get("results", []) if isinstance(r, dict)
            and (r.get("execution_state") or r.get("status", "")) in VCP_ACTIONABLE_STATES]

def _sanitize_scraped(text, max_len=60):
    """Scraped text goes into the Claude prompt — keep it to plain name chars."""
    return re.sub(r"[^A-Za-z0-9 .,'\-]", "", str(text))[:max_len]

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
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8")
        tickers = re.findall(r'stock\.php\?sym=([A-Z.\-]+)"', html)[:10]
        logger.info("Buffett holdings: %s", tickers[:5])
        return tickers
    except Exception as e:
        logger.warning("Buffett holdings failed: %s", e)
        return []

def claude_analyze(claude_client, dashboard, vcp_list, ticker, price, congress_buys, buffett_holdings):
    vcp_info = next((v for v in vcp_list
                     if (v.get("symbol") or v.get("ticker", "")) == ticker), {})
    congress_buying = [t for t in congress_buys if t.get("ticker","").upper() == ticker.upper()]
    buffett_owns = ticker in buffett_holdings

    insider_context = ""
    if congress_buying:
        names = [_sanitize_scraped(t.get("politician", t.get("name", "Unknown")))
                 for t in congress_buying[:3]]
        insider_context += f"\nCONGRESS BUYING {ticker}: {', '.join(names)}"
    if buffett_owns:
        insider_context += f"\nWARREN BUFFETT holds {ticker} in Berkshire portfolio"
    if not insider_context:
        insider_context = "\nNo notable insider/political buying found"

    # Field names differ across screener schema generations — fall back gracefully.
    state = vcp_info.get("execution_state") or vcp_info.get("status") or "Unknown"
    score = vcp_info.get("composite_score", vcp_info.get("score", "N/A"))
    rating = vcp_info.get("rating", vcp_info.get("contractions", "N/A"))
    pivot_dist = vcp_info.get("distance_from_pivot_pct")
    if pivot_dist is None and vcp_info.get("pivot_price"):
        try:
            pivot = float(vcp_info["pivot_price"])
            pivot_dist = round((price - pivot) / pivot * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pivot_dist = "N/A"
    if pivot_dist is None:
        pivot_dist = "N/A"

    prompt = f"""You are a professional momentum trader using Minervini VCP + Munger principles + political insider data.

MARKET DASHBOARD:
{dashboard[:1500]}

VCP SETUP FOR {ticker}:
- Price: ${price:.2f}
- State: {state}
- Score: {score}
- Rating: {rating}
- Pivot Distance: {pivot_dist}%

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
        decision = str(result.get("decision", "")).upper()
        confidence = result.get("confidence", 0)
        if decision not in ("BUY", "SKIP") or not isinstance(confidence, (int, float)) \
                or not 0 <= confidence <= 10:
            raise ValueError(f"malformed analysis: decision={decision!r} confidence={confidence!r}")
        result["decision"] = decision
        result["confidence"] = int(confidence)
        logger.info("%s on %s: %s (%s/10) insider=%s — %s",
                    ANTHROPIC_MODEL, ticker, result["decision"], result["confidence"],
                    result.get("insider_signal"), result.get("reason"))
        return result
    except Exception as e:
        logger.error("Claude failed: %s", e)
        return {"decision": "SKIP", "confidence": 0, "reason": "Analysis failed"}

def load_state():
    """Load {date: {"tickers_bought": [ticker, ...]}} from disk."""
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

def daily_pnl_circuit_breaker(client, state):
    """Return (ok, pct_change) — ok=False means we should NOT trade today."""
    today = date.today().isoformat()
    acct = client.get_account()
    equity = float(acct.equity)
    last_equity = float(acct.last_equity)
    if last_equity <= 0:
        return True, 0.0
    change = (equity - last_equity) / last_equity
    if change <= -DAILY_LOSS_CIRCUIT_PCT:
        logger.warning("Circuit breaker: equity down %.2f%% today (limit %.2f%%). Halting.",
                       change * 100, DAILY_LOSS_CIRCUIT_PCT * 100)
        return False, change
    return True, change

def wait_for_fill(client, order_id, timeout=FILL_WAIT_SECS, poll=3):
    """Block until the order fills (or dies/times out). Returns filled qty."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            o = client.get_order_by_id(order_id)
            if o.status.value == "filled":
                return int(float(o.filled_qty or 0))
            if o.status.value in ("canceled", "expired", "rejected", "done_for_day"):
                logger.warning("Order %s ended %s (filled %s)", order_id, o.status.value, o.filled_qty)
                return int(float(o.filled_qty or 0))
        except Exception as e:
            logger.warning("poll order %s failed: %s", order_id, e)
            break
        time.sleep(poll)
    logger.warning("Order %s not filled in %ds, canceling", order_id, timeout)
    try:
        client.cancel_order_by_id(order_id)
    except Exception:
        pass
    try:
        o = client.get_order_by_id(order_id)
        return int(float(o.filled_qty or 0))
    except Exception:
        return 0

def attach_stop(client, ticker, qty, stop_price, parent_order_id, attempts=3):
    """Attach a GTC stop-loss to a filled position. Returns order or None."""
    if DRY_RUN:
        logger.info("  [DRY-RUN] would attach stop @ $%.2f for %s", stop_price, ticker)
        return None
    for attempt in range(1, attempts + 1):
        try:
            from alpaca.trading.requests import StopOrderRequest
            stop_order = client.submit_order(
                StopOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=OrderSide.SELL,
                    stop_price=round(stop_price, 2),
                    time_in_force=TimeInForce.GTC,
                    client_order_id=f"sl-{parent_order_id}",
                )
            )
            logger.info("  ↳ Stop-loss attached @ $%.2f (ID:%s)", stop_price, stop_order.id)
            return stop_order
        except Exception as e:
            logger.error("  ↳ Stop-loss attach FAILED for %s (attempt %d/%d): %s",
                         ticker, attempt, attempts, e)
            if attempt < attempts:
                time.sleep(2)
    return None

def run():
    logger.info("=== Auto Trader + %s + Insider Tracker | %s ===",
                ANTHROPIC_MODEL, datetime.now().strftime("%Y-%m-%d %H:%M"))

    # Auto-connect chain: detect env, validate creds, write state
    logger.info("Running Alpaca auto-connect chain...")
    chain_rc = alpaca_auto_connect(dry_run=DRY_RUN, json_output=False)
    if chain_rc != 0:
        logger.error("Alpaca auto-connect failed (exit %d). Aborting.", chain_rc)
        return

    client, _ = connect_alpaca()
    data_client = _make_data_client()
    claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    if not verify_anthropic(claude_client):
        return

    if not client.get_clock().is_open:
        logger.info("Market CLOSED. No trades."); return

    state = load_state()
    today = date.today().isoformat()
    today_bought = set(state.get(today, {}).get("tickers_bought", []))

    # Daily loss circuit breaker
    ok, change = daily_pnl_circuit_breaker(client, state)
    if not ok:
        logger.info("Daily P&L %.2f%%. Skipping all trades.", change * 100)
        return

    dashboard = load_dashboard()
    if not dashboard:
        logger.error("No fresh dashboard."); return

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
    for vcp in vcps:
        if traded >= MAX_TRADES_PER_RUN:
            break
        ticker = vcp.get("symbol") or vcp.get("ticker", "")
        if not ticker: continue

        # Skip if we already bought this ticker today
        if ticker in today_bought:
            logger.info("%s: already bought today, skip", ticker); continue

        # Skip if we already hold it.
        try:
            client.get_open_position(ticker)
            logger.info("%s: already holding, skip", ticker); continue
        except Exception as e:
            err_str = str(e)
            if "does not exist" not in err_str.lower():
                logger.error("%s: position check failed (%s), skip for safety", ticker, e)
                continue
            # "does not exist" → no position, good to buy

        try:
            price = _latest_price(data_client, ticker)
            if not price:
                logger.error("%s price failed: no data", ticker); continue
        except Exception as e:
            logger.error("%s price failed: %s", ticker, e); continue

        analysis = claude_analyze(claude_client, dashboard, vcps, ticker, price,
                                  congress_buys, buffett_holdings)

        if analysis.get("decision") != "BUY":
            logger.info("%s: SKIP — %s", ticker, analysis.get("reason")); continue

        if analysis.get("confidence", 0) < CONFIDENCE_MIN:
            logger.info("%s: confidence %s/10 below %d, skip",
                        ticker, analysis.get("confidence"), CONFIDENCE_MIN); continue

        # Re-fetch cash each iteration so trade N is sized on post-trade-(N-1) cash
        try:
            cash = float(client.get_account().cash)
        except Exception as e:
            logger.error("Account refresh failed: %s", e); break

        stop = round(price * (1 - STOP_LOSS_PCT), 2)
        risk_per_share = price - stop
        if risk_per_share <= 0:
            logger.warning("%s: invalid stop calc, skip", ticker); continue
        shares = int((cash * RISK_PCT) / risk_per_share)
        shares = min(shares, int(cash * MAX_POS_PCT / price))
        if shares < 1:
            logger.warning("%s: shares=0, skip", ticker); continue

        limit_price = round(price * (1 + LIMIT_BUFFER_PCT), 2)

        if DRY_RUN:
            logger.info("[DRY-RUN] would BUY %d %s @ limit $%.2f | Stop $%.2f | Claude:%s/10 | Insider:%s",
                        shares, ticker, limit_price, stop,
                        analysis.get("confidence"), analysis.get("insider_signal"))
            today_bought.add(ticker)
            state.setdefault(today, {})["tickers_bought"] = sorted(today_bought)
            save_state(state)
            traded += 1
            continue

        try:
            order_req = LimitOrderRequest(
                symbol=ticker, qty=shares, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
            )
            order = client.submit_order(order_req)
            filled = wait_for_fill(client, str(order.id))
            if filled < 1:
                logger.warning("%s: no fill, nothing to protect, moving on", ticker)
                continue
            logger.info("✅ BUY %d %s @ limit $%.2f | Stop $%.2f | Claude:%s/10 | Insider:%s | ID:%s",
                        filled, ticker, limit_price, stop,
                        analysis.get("confidence"), analysis.get("insider_signal"), order.id)

            sl = attach_stop(client, ticker, filled, stop, str(order.id))
            if sl is None:
                logger.error("%s: stop attach failed — closing position to avoid "
                             "unprotected exposure", ticker)
                try:
                    client.close_position(ticker, ClosePositionRequest(qty=str(filled)))
                    logger.info("%s: position closed", ticker)
                except Exception as e:
                    logger.critical("%s: EMERGENCY CLOSE FAILED (%s) — MANUAL "
                                    "INTERVENTION REQUIRED", ticker, e)

            today_bought.add(ticker)
            state.setdefault(today, {})["tickers_bought"] = sorted(today_bought)
            save_state(state)

            traded += 1
        except Exception as e:
            logger.error("%s order failed: %s", ticker, e)

    logger.info("=== %d trade(s) placed ===", traded)


# ── Helpers for alpaca-py SDK ──────────────────────────────────────────────

def _make_data_client():
    """Build a StockHistoricalDataClient for latest-trade queries."""
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def _latest_price(data_client, symbol: str) -> float:
    """Return the last trade price for symbol via IEX feed."""
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        trade_data = data_client.get_stock_latest_trade(req)
        trade = trade_data[symbol]
        return float(trade.price)
    except Exception:
        return 0.0


if __name__ == "__main__":
    run()
