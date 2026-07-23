"""
Auto Trader — VCP + Mean-Reversion + Claude analysis + Congress/Buffett insider tracking.

Supports three modes (set AUTO_TRADER_MODE env or --mode flag):
  vcp     — Minervini VCP breakout (default)
  meanrev — RSI oversold + Bollinger pullback (no FMP, no API key needed)
  both    — run VCP then meanrev in one pass

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
MIN_FTD_SCORE = int(os.environ.get("MIN_FTD_SCORE", "80"))
MODE          = os.environ.get("AUTO_TRADER_MODE", "vcp").lower()
MEANREV_MIN_RSI = float(os.environ.get("MEANREV_MIN_RSI", "40"))   # must be below this to qualify
MAX_POS_PCT   = 0.25
MAX_TRADES_PER_RUN = 2
STOP_LOSS_PCT = 0.07        # 7% trailing-style stop
STOP_LOSS_PCT_MR = 0.10     # mean rev can use wider stop since entries are pullbacks
TAKE_PROFIT_PCT_VCP = 0.10  # 10% target for VCP breakouts
TAKE_PROFIT_PCT_MR  = 0.05  # 5% target for mean reversion snap-back
TRAIL_PCT_VCP       = 0.05  # 5% trailing stop for VCP (lets winners run)
TRAIL_PCT_MR        = 0.05  # 5% trailing stop for MeanRev
LIMIT_BUFFER_PCT    = 0.005 # marketable-limit entry: cap slippage at 0.5%
VIX_MIN             = float(os.environ.get("VIX_MIN", "15"))       # skip if market too calm
EARNINGS_BUFFER_DAYS = int(os.environ.get("EARNINGS_BUFFER_DAYS", "10"))
VOLUME_RATIO_MIN    = float(os.environ.get("VOLUME_RATIO_MIN", "0.7"))
CONFIDENCE_MIN = 6
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

def load_meanrev():
    """Run the core meanrev screener (yfinance, no API key). Returns candidates."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "core"))
        from meanrev_screener import screen as _screen_meanrev
        results = _screen_meanrev()
        logger.info(f"MeanRev: {len(results)} candidates from screener")
        for r in results[:5]:
            logger.info(f"  {r['symbol']} RSI={r['rsi']} BBpos={r['bb_position']:.0f}%"
                        f" sma50=${r['sma50']} score={r['score']}")
        return results
    except Exception as e:
        logger.error("MeanRev screener failed: %s", e)
        return []

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
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36",
        })
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
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36",
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

def get_vix() -> float:
    """Fetch current VIX level via yfinance. Returns 20.0 on failure."""
    try:
        import yfinance as yf
        data = yf.download("^VIX", period="2d", interval="1d", progress=False, auto_adjust=True)
        return float(data["Close"].squeeze().dropna().iloc[-1])
    except Exception as e:
        logger.warning("VIX fetch failed: %s", e)
        return 20.0


def get_spy_regime() -> bool:
    """Paul Tudor Jones rule: only buy when SPY is above its 200-day MA.
    Returns True (safe to trade) or False (bear market — skip all buys)."""
    try:
        import yfinance as yf
        data = yf.download("SPY", period="1y", interval="1d", progress=False, auto_adjust=True)
        closes = data["Close"].squeeze().dropna().tolist()
        if len(closes) < 200:
            return True  # not enough history — don't block
        sma200 = sum(closes[-200:]) / 200
        price  = closes[-1]
        in_uptrend = price > sma200
        logger.info("SPY regime: $%.2f vs SMA200 $%.2f → %s",
                    price, sma200, "BULL" if in_uptrend else "BEAR")
        return in_uptrend
    except Exception as e:
        logger.warning("SPY regime check failed: %s", e)
        return True


def get_rsi2(ticker: str) -> float:
    """Larry Connors RSI(2): 2-day RSI for extreme oversold timing.
    Returns 50.0 on failure (neutral — won't block the trade)."""
    try:
        import yfinance as yf
        data = yf.download(ticker, period="10d", interval="1d", progress=False, auto_adjust=True)
        closes = data["Close"].squeeze().dropna().tolist()
        if len(closes) < 3:
            return 50.0
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas[-2:]]
        losses = [abs(min(d, 0)) for d in deltas[-2:]]
        avg_g  = sum(gains) / 2
        avg_l  = sum(losses) / 2
        if avg_l == 0:
            return 100.0
        rsi2 = 100 - (100 / (1 + avg_g / avg_l))
        logger.info("  %s RSI(2)=%.1f", ticker, rsi2)
        return round(rsi2, 1)
    except Exception as e:
        logger.warning("RSI(2) %s failed: %s", ticker, e)
        return 50.0


def rank_by_relative_strength(candidates: list, period_days: int = 126) -> list:
    """O'Neil relative strength: sort candidates by 6-month return vs SPY.
    Strongest relative performers come first — buy the best stock in a dip."""
    try:
        import yfinance as yf
        tickers = [c.get("symbol", "") for c in candidates if c.get("symbol")]
        if not tickers:
            return candidates
        syms = tickers + ["SPY"]
        data = yf.download(syms, period="7mo", interval="1d",
                           progress=False, auto_adjust=True, group_by="ticker")
        spy_closes = data["SPY"]["Close"].dropna().tolist() if "SPY" in data.columns.get_level_values(0) else []
        spy_ret = (spy_closes[-1] / spy_closes[-period_days] - 1) if len(spy_closes) >= period_days else 0.0

        rs_map: dict[str, float] = {}
        for sym in tickers:
            try:
                closes = data[sym]["Close"].dropna().tolist()
                if len(closes) >= period_days:
                    stock_ret = closes[-1] / closes[-period_days] - 1
                    rs_map[sym] = round((stock_ret - spy_ret) * 100, 2)
                else:
                    rs_map[sym] = 0.0
            except Exception:
                rs_map[sym] = 0.0

        for c in candidates:
            c["rs_vs_spy"] = rs_map.get(c.get("symbol", ""), 0.0)

        ranked = sorted(candidates, key=lambda x: x.get("rs_vs_spy", 0.0), reverse=True)
        logger.info("RS rank: %s", [(c["symbol"], f"{c['rs_vs_spy']:+.1f}%") for c in ranked])
        return ranked
    except Exception as e:
        logger.warning("RS ranking failed: %s", e)
        return candidates


def get_earnings_risk_tickers() -> set:
    """Return tickers with earnings within EARNINGS_BUFFER_DAYS days (via FMP)."""
    fmp_key = os.environ.get("FMP_API_KEY", "")
    if not fmp_key:
        logger.warning("No FMP_API_KEY — earnings filter skipped")
        return set()
    try:
        from datetime import timedelta
        from_d = date.today().isoformat()
        to_d   = (date.today() + timedelta(days=EARNINGS_BUFFER_DAYS)).isoformat()
        url = (f"https://financialmodelingprep.com/api/v3/earning_calendar"
               f"?from={from_d}&to={to_d}&apikey={fmp_key}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        tickers = {e["symbol"] for e in data if isinstance(e, dict)}
        logger.info("Earnings risk: %d tickers with earnings in next %dd", len(tickers), EARNINGS_BUFFER_DAYS)
        return tickers
    except Exception as e:
        logger.warning("Earnings filter failed: %s", e)
        return set()


def check_weekly_uptrend(ticker: str) -> bool:
    """Return True if the weekly chart is in uptrend (price > weekly SMA20, weekly RSI > 40)."""
    try:
        import yfinance as yf
        data = yf.download(ticker, period="2y", interval="1wk",
                           progress=False, auto_adjust=True)
        closes = data["Close"].squeeze().dropna().tolist()
        if len(closes) < 20:
            return True  # not enough history — don't filter out
        sma20 = sum(closes[-20:]) / 20
        # Weekly RSI(14)
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas[-14:]]
        losses = [abs(min(d, 0)) for d in deltas[-14:]]
        avg_g  = sum(gains) / 14
        avg_l  = sum(losses) / 14
        weekly_rsi = 100 - (100 / (1 + avg_g / avg_l)) if avg_l else 100.0
        in_uptrend = closes[-1] >= sma20 * 0.95 and weekly_rsi > 40
        logger.info("  %s weekly: RSI=%.1f SMA20=$%.2f uptrend=%s",
                    ticker, weekly_rsi, sma20, in_uptrend)
        return in_uptrend
    except Exception as e:
        logger.warning("Weekly check %s failed: %s", ticker, e)
        return True


def attach_trailing_stop(client, ticker, qty, trail_pct, parent_order_id, attempts=3):
    """Attach a trailing stop (GTC). Falls back to fixed limit sell for fractional."""
    if DRY_RUN:
        logger.info("  [DRY-RUN] would attach trailing stop %.1f%% for %s", trail_pct * 100, ticker)
        return None
    # Try trailing stop (works for whole shares)
    for attempt in range(1, attempts + 1):
        try:
            from alpaca.trading.requests import TrailingStopOrderRequest
            ts = client.submit_order(TrailingStopOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                trail_percent=round(trail_pct * 100, 1),
                time_in_force=TimeInForce.GTC,
            ))
            logger.info("  ↳ Trailing stop %.1f%% attached (ID:%s)", trail_pct * 100, ts.id)
            return ts
        except Exception as e:
            logger.error("  ↳ Trailing stop FAILED (attempt %d/%d): %s", attempt, attempts, e)
            if attempt < attempts:
                time.sleep(2)
    return None


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
        # Parse JSON by brace-depth so stray closing braces don't truncate.
        start = text.find("{")
        if start == -1:
            raise ValueError("No JSON object in Claude response")
        depth = 0
        for i, ch in enumerate(text[start:]):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    result = json.loads(text[start:start + i + 1])
                    break
        else:
            raise ValueError("Unmatched braces in Claude response")
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

def claude_analyze_meanrev(claude_client, dashboard, mr_list, ticker, price,
                            congress_buys, buffett_holdings):
    """Claude prompt for mean-reversion candidates."""
    mr_info = next((m for m in mr_list
                    if (m.get("symbol") or m.get("ticker", "")) == ticker), {})
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

    rsi    = mr_info.get("rsi", "N/A")
    bb_pos = mr_info.get("bb_position", "N/A")
    sma50  = mr_info.get("sma50", 0)
    sma200 = mr_info.get("sma200", 0)
    mom    = mr_info.get("momentum_pct", 0)
    target = sma50 if sma50 > price else round(price * 1.03, 2)
    score  = mr_info.get("score", "N/A")

    prompt = f"""You are a professional mean-reversion trader: buy oversold stocks that snap back to their
20/50-day moving average. Use RSI, Bollinger Bands, and institutional ownership for conviction.

MARKET DASHBOARD:
{dashboard[:1500]}

MEAN-REVERSION SETUP FOR {ticker}:
- Price: ${price:.2f}
- RSI(14): {rsi} (oversold < 40)
- Bollinger Band position: {bb_pos}% above lower band
- SMA50 (target): ${sma50}
- SMA200: ${sma200}
- 20d momentum: {mom}%
- Reversion score: {score}
- Expected target: ${target:.2f} (~{round((target - price) / price * 100, 1)}% gain to mean)

POLITICAL & INSIDER DATA:{insider_context}

Consider: Is this a genuine pullback in an uptrend (buy), or the start of a breakdown (skip)?
Look at RSI depth, BB position, volume trend, and who else is buying.

BUY {ticker} for mean-reversion right now? Consider technicals + market + insider data.

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
        start = text.find("{")
        if start == -1:
            raise ValueError("No JSON object in Claude response")
        depth = 0
        for i, ch in enumerate(text[start:]):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    result = json.loads(text[start:start + i + 1])
                    break
        else:
            raise ValueError("Unmatched braces in Claude response")
        decision = str(result.get("decision", "")).upper()
        confidence = result.get("confidence", 0)
        if decision not in ("BUY", "SKIP") or not isinstance(confidence, (int, float)) \
                or not 0 <= confidence <= 10:
            raise ValueError(f"malformed analysis: decision={decision!r} confidence={confidence!r}")
        result["decision"] = decision
        result["confidence"] = int(confidence)
        logger.info("%s on %s (meanrev): %s (%s/10) insider=%s — %s",
                    ANTHROPIC_MODEL, ticker, result["decision"], result["confidence"],
                    result.get("insider_signal"), result.get("reason"))
        return result
    except Exception as e:
        logger.error("Claude meanrev failed: %s", e)
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

def attach_take_profit(client, ticker, qty, tp_price, parent_order_id, attempts=3):
    """Attach a limit sell take-profit to a filled position. Tries GTC then DAY for fractional."""
    if DRY_RUN:
        logger.info("  [DRY-RUN] would attach take-profit @ $%.2f for %s", tp_price, ticker)
        return None
    for tif in (TimeInForce.GTC, TimeInForce.DAY):
        for attempt in range(1, attempts + 1):
            try:
                tp_order = client.submit_order(
                    LimitOrderRequest(
                        symbol=ticker,
                        qty=qty,
                        side=OrderSide.SELL,
                        limit_price=round(tp_price, 2),
                        time_in_force=tif,
                    )
                )
                logger.info("  ↳ Take-profit attached @ $%.2f %s (ID:%s)", tp_price, tif.value, tp_order.id)
                return tp_order
            except Exception as e:
                logger.error("  ↳ Take-profit %s FAILED for %s (attempt %d/%d): %s",
                             tif.value, ticker, attempt, attempts, e)
                if attempt < attempts:
                    time.sleep(2)
                else:
                    break  # try next tif
    logger.warning("  ↳ Take-profit not attached for %s — sell manually at $%.2f", ticker, tp_price)
    return None


def _already_open_or_bought(client, ticker, today_bought):
    """Check if ticker is already held or already bought today."""
    if ticker in today_bought:
        return True, "already bought today"
    try:
        client.get_open_position(ticker)
        return True, "already holding"
    except Exception as e:
        if "does not exist" not in str(e).lower():
            return True, f"position check failed: {e}"
    return False, ""

def _place_trade(client, data_client, ticker, price, stop_pct, take_profit_pct, analysis,
                  traded, congress_buys, buffett_holdings, state, today,
                  today_bought):
    """Size, submit, and attach stop for one entry. Returns (filled, new_today_bought)."""
    from alpaca.trading.requests import MarketOrderRequest
    cash = float(client.get_account().cash)
    stop  = round(price * (1 - stop_pct), 2)
    risk  = price - stop
    if risk <= 0:
        logger.warning("  %s: invalid stop calc (risk=%.2f), skip", ticker, risk); return 0, today_bought

    shares = int((cash * RISK_PCT) / risk)
    shares = min(shares, int(cash * MAX_POS_PCT / price))

    # Small account: use fractional/notional order when cash can't afford a whole share
    use_notional = shares < 1
    notional = round(cash * MAX_POS_PCT, 2) if use_notional else 0
    if use_notional and notional < 1:
        logger.warning("  %s: account too small (cash=%.2f), skip", ticker, cash); return 0, today_bought

    limit_price = round(price * (1 + LIMIT_BUFFER_PCT), 2)
    take_profit = round(price * (1 + take_profit_pct), 2)

    if DRY_RUN:
        if use_notional:
            logger.info("[DRY-RUN] would BUY $%.2f notional %s @ market | Stop $%.2f | TP $%.2f | "
                        "Claude:%s/10",
                        notional, ticker, stop, take_profit, analysis.get("confidence"))
        else:
            logger.info("[DRY-RUN] would BUY %d %s @ limit $%.2f | Stop $%.2f | TP $%.2f | "
                        "Claude:%s/10",
                        shares, ticker, limit_price, stop, take_profit, analysis.get("confidence"))
        return 1, today_bought | {ticker}

    try:
        if use_notional:
            # Fractional share via notional market order
            order_req = MarketOrderRequest(
                symbol=ticker, notional=notional, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            order = client.submit_order(order_req)
            filled_qty = wait_for_fill(client, str(order.id))
            # Get actual fractional qty from position
            try:
                pos = client.get_open_position(ticker)
                filled_qty_f = float(pos.qty)
            except Exception:
                filled_qty_f = notional / price
            logger.info("✅ BUY $%.2f notional %s (~%.4f shares) | Stop $%.2f | TP $%.2f | "
                        "Claude:%s/10 | ID:%s",
                        notional, ticker, filled_qty_f, stop, take_profit,
                        analysis.get("confidence"), order.id)
            # Alpaca doesn't support GTC stops on fractional — skip stop, attach take-profit only
            attach_take_profit(client, ticker, round(filled_qty_f, 9), take_profit, str(order.id))
        else:
            order_req = LimitOrderRequest(
                symbol=ticker, qty=shares, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
            )
            order = client.submit_order(order_req)
            filled_qty = wait_for_fill(client, str(order.id))
            if filled_qty < 1:
                logger.warning("%s: no fill, nothing to protect", ticker); return 0, today_bought
            logger.info("✅ BUY %d %s @ limit $%.2f | Stop $%.2f | TP $%.2f | "
                        "Claude:%s/10 | ID:%s",
                        filled_qty, ticker, limit_price, stop, take_profit,
                        analysis.get("confidence"), order.id)
            attach_trailing_stop(client, ticker, filled_qty, stop_pct, str(order.id))
            attach_take_profit(client, ticker, filled_qty, take_profit, str(order.id))
        return 1, today_bought | {ticker}
    except Exception as e:
        logger.error("%s order failed: %s", ticker, e); return 0, today_bought


def _live_confirm(client, mode_str):
    """
    Block and ask for human confirmation if running against a LIVE account.
    Returns True to proceed, False to abort.
    """
    paper_mode = os.environ.get("ALPACA_PAPER", os.environ.get("ALPACA_PAPER_TRADE", "true")).lower() == "true"
    dry        = DRY_RUN  # read the compiled flag (already set at module level)

    if paper_mode or dry:
        return True  # safe — not live

    acct = client.get_account()
    equity = float(acct.equity)
    cash   = float(acct.cash)
    logger.info("\n⚠️  LIVE ACCOUNT — %s MODE", os.environ.get("AUTO_TRADER_MODE", "?").upper())
    logger.info("   Equity: $%s  |  Cash: $%s", f"{equity:,.2f}", f"{cash:,.2f}")
    print()
    print("=" * 60)
    print("  🚨  LIVE TRADING — REAL MONEY WILL BE AT RISK  🚨")
    print("=" * 60)
    print(f"  Mode:   {mode_str}")
    print(f"  Equity: ${equity:,.2f}  |  Cash: ${cash:,.2f}")
    print(f"  Slots:  1 VCP + 1 MeanRev (max 2 trades)")
    print(f"  Risk:   1% per trade  |  Stops: VCP 7%, MR 10%")
    print("=" * 60)
    try:
        reply = input("  Type YES (all caps) to confirm and place real orders: ").strip()
    except (EOFError, OSError):
        reply = ""
    if reply != "YES":
        print("  Aborted — no orders placed.")
        return False
    print("  Confirmed — proceeding with live orders.\n")
    return True


def run():
    mode     = os.environ.get("AUTO_TRADER_MODE", "vcp").lower()
    mode_str = {"vcp": "VCP", "meanrev": "MeanRev", "both": "VCP+MeanRev"}.get(mode, mode.upper())
    logger.info("=== Auto Trader [%s] + Anthropic:%s | %s ===",
                mode_str, ANTHROPIC_MODEL, datetime.now().strftime("%Y-%m-%d %H:%M"))

    # Auto-connect chain
    chain_rc = alpaca_auto_connect(dry_run=DRY_RUN, json_output=False)
    if chain_rc != 0:
        logger.error("Alpaca auto-connect failed (exit %d). Aborting.", chain_rc); return

    client, _  = connect_alpaca()
    data_client = _make_data_client()
    claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    if not verify_anthropic(claude_client): return

    if not client.get_clock().is_open:
        logger.info("Market CLOSED. No trades."); return

    # Human confirmation for live accounts
    if not _live_confirm(client, mode_str):
        return

    state = load_state()
    today = date.today().isoformat()
    today_bought = set(state.get(today, {}).get("tickers_bought", []))

    ok, change = daily_pnl_circuit_breaker(client, state)
    if not ok:
        logger.info("Daily P&L %.2f%%. Skipping all trades.", change * 100); return

    # VIX gate — only trade when market fear is elevated enough
    vix = get_vix()
    logger.info("VIX: %.1f (min=%.1f)", vix, VIX_MIN)
    if vix < VIX_MIN:
        logger.info("VIX %.1f < %.1f — market too calm, edge insufficient. Skipping.", vix, VIX_MIN)
        return

    # Paul Tudor Jones regime gate — never buy in a bear market
    if not get_spy_regime():
        logger.info("SPY below 200-day MA — bear market regime. Skipping all buys.")
        return

    # Load market dashboard only when VCP is involved
    dashboard = ""
    if mode in ("vcp", "both"):
        dashboard = load_dashboard()
        if not dashboard:
            logger.error("No fresh dashboard. Aborting VCP load."); dashboard = ""
        else:
            ftd = parse_ftd_score(dashboard)
            logger.info("FTD Score: %.1f | Threshold: %d", ftd, MIN_FTD_SCORE)
            if ftd < MIN_FTD_SCORE:
                logger.info("FTD below %d — VCP blocked.", MIN_FTD_SCORE)
            else:
                logger.info("FTD threshold MET — VCP eligible.")

    # Load candidates
    vcps = []
    meanrevs = []
    if mode in ("vcp", "both") and ftd >= MIN_FTD_SCORE:
        vcps = load_vcp()
        if vcps:
            logger.info("%d VCP setups loaded", len(vcps))
        else:
            logger.info("No VCP setups.")

    if mode in ("meanrev", "both"):
        meanrevs = load_meanrev()
        if meanrevs:
            logger.info("%d MeanRev setups loaded", len(meanrevs))
        else:
            logger.info("No MeanRev setups.")

    if not vcps and not meanrevs:
        logger.info("No candidates from any strategy. Done."); return

    # Fetch insider data once
    congress_buys   = get_congress_trades()
    buffett_holdings = get_buffett_holdings()

    traded = 0

    # ── Slot allocation: split 1 VCP + 1 MeanRev (capped at MAX_TRADES) ─────
    vcp_slots     = 1 if mode in ("vcp", "both") and vcps else 0
    mr_slots      = 1 if mode in ("meanrev", "both") and meanrevs else 0
    # Preserve existing tickers already bought so neither strategy re-trades them
    vcp_done = set(today_bought)
    mr_done  = set(today_bought)

    # ── VCP sweep ─────────────────────────────────────────────────────────────
    if mode in ("vcp", "both") and vcps and ftd >= MIN_FTD_SCORE:
        logger.info("--- VCP SWEEP (%d candidates, %d slot) ---", len(vcps), vcp_slots)
        for vcp in vcps:
            if vcp_slots <= 0: break
            if traded >= MAX_TRADES_PER_RUN: break

            ok, _ = daily_pnl_circuit_breaker(client, state)
            if not ok:
                logger.warning("Circuit breaker mid-VCP — halting at %d trades", traded); break

            ticker = vcp.get("symbol") or vcp.get("ticker", "")
            if not ticker: continue
            skip, why = _already_open_or_bought(client, ticker, today_bought)
            if skip:
                logger.info("%s: %s, skip", ticker, why); continue

            price = _latest_price(data_client, ticker)
            if not price:
                logger.error("%s price failed, skip", ticker); continue

            analysis = claude_analyze(claude_client, dashboard, vcps, ticker, price,
                                      congress_buys, buffett_holdings)
            if analysis.get("decision") != "BUY":
                logger.info("%s: SKIP — %s", ticker, analysis.get("reason")); continue
            if analysis.get("confidence", 0) < CONFIDENCE_MIN:
                logger.info("%s: confidence %s/10 below %d, skip",
                            ticker, analysis.get("confidence"), CONFIDENCE_MIN); continue

            filled, today_bought = _place_trade(
                client, data_client, ticker, price,
                STOP_LOSS_PCT, TAKE_PROFIT_PCT_VCP,
                analysis, traded, congress_buys, buffett_holdings,
                state, today, today_bought,
            )
            if filled:
                traded   += 1
                vcp_slots -= 1
                state.setdefault(today, {})["tickers_bought"] = sorted(today_bought)
                save_state(state)

    # ── MeanRev sweep ─────────────────────────────────────────────────────────
    if mode in ("meanrev", "both") and meanrevs:
        # O'Neil relative strength: buy the best stock in a dip, not the worst
        meanrevs = rank_by_relative_strength(meanrevs)
        earnings_risk = get_earnings_risk_tickers()
        logger.info("--- MEANREV SWEEP (%d candidates, %d slot) ---", len(meanrevs), mr_slots)
        for mr in meanrevs:
            if mr_slots <= 0: break
            if traded >= MAX_TRADES_PER_RUN: break

            ok, _ = daily_pnl_circuit_breaker(client, state)
            if not ok:
                logger.warning("Circuit breaker mid-MeanRev — halting at %d trades", traded); break

            ticker = mr.get("symbol") or mr.get("ticker", "")
            if not ticker: continue
            skip, why = _already_open_or_bought(client, ticker, today_bought)
            if skip:
                logger.info("%s: %s, skip", ticker, why); continue

            # Volume confirmation: recent volume must be at least VOLUME_RATIO_MIN of avg
            vol_ratio = mr.get("volume_ratio", 1.0)
            if vol_ratio < VOLUME_RATIO_MIN:
                logger.info("%s: vol_ratio=%.2f < %.2f (low volume) — skip",
                            ticker, vol_ratio, VOLUME_RATIO_MIN); continue

            # Earnings proximity filter: skip if earnings due within EARNINGS_BUFFER_DAYS
            if ticker in earnings_risk:
                logger.info("%s: earnings in next %dd — skip", ticker, EARNINGS_BUFFER_DAYS); continue

            price = _latest_price(data_client, ticker)
            if not price:
                logger.error("%s price failed, skip", ticker); continue

            # MeanRev-specific minimum RSI filter
            rsi = mr.get("rsi", 99)
            if rsi >= MEANREV_MIN_RSI:
                logger.info("%s: RSI=%.1f not oversold (need <%.0f), skip",
                            ticker, rsi, MEANREV_MIN_RSI); continue

            # Weekly chart filter: only mean-revert within a larger uptrend
            if not check_weekly_uptrend(ticker):
                logger.info("%s: weekly chart not in uptrend — skip", ticker); continue

            # Larry Connors RSI(2): extreme oversold timing — skip if not deeply washed out
            rsi2 = get_rsi2(ticker)
            if rsi2 > 15:
                logger.info("%s: RSI(2)=%.1f > 15 — not extreme enough (Connors filter), skip",
                            ticker, rsi2); continue

            analysis = claude_analyze_meanrev(claude_client, dashboard, meanrevs,
                                              ticker, price, congress_buys, buffett_holdings)
            if analysis.get("decision") != "BUY":
                logger.info("%s: SKIP — %s", ticker, analysis.get("reason")); continue
            if analysis.get("confidence", 0) < CONFIDENCE_MIN:
                logger.info("%s: confidence %s/10 below %d, skip",
                            ticker, analysis.get("confidence"), CONFIDENCE_MIN); continue

            filled, today_bought = _place_trade(
                client, data_client, ticker, price,
                STOP_LOSS_PCT_MR, TAKE_PROFIT_PCT_MR,
                analysis, traded, congress_buys, buffett_holdings,
                state, today, today_bought,
            )
            if filled:
                traded    += 1
                mr_slots   -= 1
                state.setdefault(today, {})["tickers_bought"] = sorted(today_bought)
                save_state(state)

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
    import argparse
    p = argparse.ArgumentParser(description="Auto Trader — VCP + Mean-Reversion")
    p.add_argument("--mode", choices=["vcp", "meanrev", "both"],
                   default=MODE, help=f"Strategy mode (default: {MODE})")
    p.add_argument("--dry-run", action="store_true",
                   help="Simulate orders without placing them")
    args = p.parse_args()
    # Propagate CLI args into environment so run() picks them up
    os.environ["AUTO_TRADER_MODE"]  = args.mode
    os.environ["AUTO_TRADER_DRY_RUN"] = "1" if args.dry_run else "0"
    run()
