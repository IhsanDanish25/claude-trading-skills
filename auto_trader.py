#!/usr/bin/env python3
"""Auto Trader — FTD + VCP → Alpaca paper trade. 1% risk."""
import os, json, logging
from pathlib import Path
from datetime import datetime
import alpaca_trade_api as tradeapi

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_URL    = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
RISK_PCT      = 0.01
MIN_FTD_SCORE = 80
DASHBOARD_DIR = Path(__file__).parent / "examples/daily-market-dashboard/knowledge"

def connect():
    api = tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, ALPACA_URL)
    acct = api.get_account()
    logger.info("Connected | Cash: $%.2f | %s", float(acct.cash), "PAPER" if "paper" in ALPACA_URL else "LIVE")
    return api, acct

def parse_ftd_score():
    files = sorted(DASHBOARD_DIR.glob("daily_dashboard_*.md"), reverse=True)
    if not files: return 0.0
    for line in files[0].read_text().splitlines():
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

def run():
    logger.info("=== Auto Trader %s ===", datetime.now().strftime("%Y-%m-%d %H:%M"))
    api, acct = connect()
    cash = float(acct.cash)

    if not api.get_clock().is_open:
        logger.info("Market CLOSED. No trades."); return

    ftd = parse_ftd_score()
    logger.info("FTD Score: %.1f", ftd)
    if ftd < MIN_FTD_SCORE:
        logger.info("FTD below %d. No trades.", MIN_FTD_SCORE); return

    vcps = load_vcp()
    if not vcps:
        logger.info("No VCP setups. No trades."); return

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
            stop  = price * 0.93
            shares = int((cash * RISK_PCT) / (price - stop))
            shares = min(shares, int(cash * 0.25 / price))
            if shares < 1: continue
            order = api.submit_order(symbol=ticker, qty=shares, side="buy",
                                     type="market", time_in_force="day")
            logger.info("BUY %d %s @ ~$%.2f | Stop $%.2f | ID: %s",
                        shares, ticker, price, stop, order.id)
            traded += 1
        except Exception as e:
            logger.error("%s failed: %s", ticker, e)

    logger.info("=== %d trade(s) placed ===", traded)

if __name__ == "__main__":
    run()
