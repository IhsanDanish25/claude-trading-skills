"""
Market researcher — builds the daily situation brief used by market_open.

Runs during pre_market (6 AM ET). Output cached in state/market_brief_<date>.json.
market_open reads the cache so no extra API calls happen during the entry window.

Data sources:
  yfinance  — sector ETF daily % change + stock-specific news headlines
  FMP       — economic calendar (if available on plan)
  Claude    — synthesize macro context + score stock news sentiment
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config

log = logging.getLogger(__name__)

SECTOR_ETFS = {
    "Technology":       "XLK",
    "Healthcare":       "XLV",
    "Financials":       "XLF",
    "Energy":           "XLE",
    "Consumer Disc":    "XLY",
    "Industrials":      "XLI",
    "Materials":        "XLB",
    "Utilities":        "XLU",
    "Real Estate":      "XLRE",
    "Comm Services":    "XLC",
    "Consumer Staples": "XLP",
}


def _fetch_sector_rotation() -> dict:
    """1-day % change for all sector ETFs via yfinance."""
    try:
        import yfinance as yf
        etfs = list(SECTOR_ETFS.values())
        data = yf.download(etfs, period="3d", interval="1d",
                           progress=False, auto_adjust=True)
        closes = data["Close"].dropna()
        if len(closes) < 2:
            return {}
        rotation = {}
        for sector, etf in SECTOR_ETFS.items():
            if etf in closes.columns:
                prev = float(closes[etf].iloc[-2])
                curr = float(closes[etf].iloc[-1])
                if prev > 0:
                    rotation[sector] = round((curr - prev) / prev * 100, 2)
        return rotation
    except Exception as e:
        log.warning("Sector rotation fetch failed: %s", e)
        return {}


def _fetch_stock_news(symbols: list[str]) -> dict[str, list[str]]:
    """Fetch recent news headlines per symbol via yfinance .news."""
    result: dict[str, list[str]] = {}
    for sym in symbols[:20]:
        try:
            import yfinance as yf
            items = yf.Ticker(sym).news or []
            headlines = []
            for item in items[:5]:
                # yfinance news format varies by version
                title = (
                    item.get("content", {}).get("title")
                    or item.get("title")
                    or ""
                )
                if title:
                    headlines.append(str(title)[:120])
            if headlines:
                result[sym] = headlines
        except Exception as e:
            log.debug("News %s: %s", sym, e)
    return result


def build_daily_brief(
    breadth: dict,
    calendar: list[dict],
    candidates: list[str],
) -> dict:
    """
    Orchestrate the full research pipeline for today.

    breadth   — output of fmp.get_market_breadth()
    calendar  — output of fmp.get_economic_calendar()
    candidates — symbols the bot may trade today (meanrev + momentum candidates)

    Returns the brief dict (also saves to state/market_brief_<date>.json).
    """
    from core.analyst import build_situation_report, check_stock_news_batch

    today = datetime.date.today().isoformat()

    # ── Sector rotation ───────────────────────────────────────────────────────
    log.info("Research: sector rotation via yfinance...")
    rotation = _fetch_sector_rotation()
    if rotation:
        hot  = sorted(rotation, key=rotation.get, reverse=True)[:3]
        cold = sorted(rotation, key=rotation.get)[:3]
        log.info("  Hot:  %s", [(s, f"{rotation[s]:+.1f}%") for s in hot])
        log.info("  Cold: %s", [(s, f"{rotation[s]:+.1f}%") for s in cold])
    else:
        hot, cold = [], []

    # ── Stock news ────────────────────────────────────────────────────────────
    log.info("Research: news for %d candidates via yfinance...", len(candidates))
    raw_news = _fetch_stock_news(candidates)
    log.info("  Headlines found for: %s", list(raw_news.keys()))

    # ── Claude: macro situation report ────────────────────────────────────────
    log.info("Research: Claude synthesizing situation report...")
    high_impact = [e for e in calendar if e.get("impact") == "High"][:5]
    situation = build_situation_report({
        "date": today,
        "breadth": {
            "spy_change_pct":  breadth.get("spy_change_pct", 0),
            "qqq_change_pct":  breadth.get("qqq_change_pct", 0),
            "iwm_change_pct":  breadth.get("iwm_change_pct", 0),
            "advancing":       breadth.get("advancing", 0),
            "declining":       breadth.get("declining", 0),
        },
        "high_impact_events_today": [
            {"event": e.get("event", ""), "time": e.get("date", ""),
             "impact": e.get("impact", "")}
            for e in high_impact
        ],
        "sector_rotation": rotation,
    })
    log.info("  Macro risk: %s | Override: %s",
             situation.get("macro_risk", "?"),
             situation.get("trade_bias_override", "none"))
    log.info("  Summary: %s", situation.get("summary", "")[:120])

    # ── Claude: score stock news ──────────────────────────────────────────────
    news_verdicts: dict = {}
    if raw_news:
        log.info("Research: Claude scoring news for %d stocks...", len(raw_news))
        news_verdicts = check_stock_news_batch(raw_news)
        skips = [s for s, v in news_verdicts.items() if v.get("skip")]
        if skips:
            log.warning("  News filter: SKIP %s", skips)
        else:
            log.info("  No stocks flagged for bad news")

    # ── Assemble brief ────────────────────────────────────────────────────────
    brief = {
        "date":                today,
        "generated_at":        datetime.datetime.utcnow().isoformat() + "Z",
        "macro_risk":          situation.get("macro_risk", "medium"),
        "trade_bias_override": situation.get("trade_bias_override"),
        "event_blocks":        situation.get("event_blocks", []),
        "summary":             situation.get("summary", ""),
        "sector_rotation":     rotation,
        "hot_sectors":         hot,
        "cold_sectors":        cold,
        "stock_news":          news_verdicts,
        "raw_headlines":       raw_news,
    }

    path = os.path.join(config.STATE_DIR, f"market_brief_{today}.json")
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(brief, f, indent=2)
    log.info("Research brief → %s", path)
    return brief


def load_today_brief() -> dict:
    """Load today's cached brief. Returns {} if not yet generated."""
    today = datetime.date.today().isoformat()
    path = os.path.join(config.STATE_DIR, f"market_brief_{today}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.warning("Could not load market brief: %s", e)
        return {}
