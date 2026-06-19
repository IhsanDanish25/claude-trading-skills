#!/usr/bin/env python3
"""News sentiment analyzer — NLP-scores news headlines bullish/bearish."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests


FMP_BASE = "https://financialmodelingprep.com/stable"

BULLISH_WORDS = {
    "surge", "soar", "rally", "beat", "strong", "upgrade", "outperform", "buy",
    "record", "high", "gain", "rise", "positive", "growth", "bullish", "breakout",
    "momentum", "exceed", "profit", "revenue", "deal", "contract", "approval",
    "launch", "expand", "increase", "accelerate", "better", "top", "win",
}

BEARISH_WORDS = {
    "drop", "fall", "crash", "miss", "weak", "downgrade", "underperform", "sell",
    "low", "loss", "decline", "negative", "shrink", "bearish", "breakdown",
    "cut", "reduce", "warning", "risk", "concern", "investigation", "lawsuit",
    "recall", "layoff", "fraud", "default", "downgrade", "miss", "disappoint",
}

INTENSIFIERS = {"very", "significantly", "sharply", "dramatically", "massively"}


def score_headline(text: str) -> float:
    words = re.findall(r"\b\w+\b", text.lower())
    score = 0.0
    intensify = False
    for w in words:
        if w in INTENSIFIERS:
            intensify = True
            continue
        multiplier = 1.5 if intensify else 1.0
        intensify = False
        if w in BULLISH_WORDS:
            score += 1.0 * multiplier
        elif w in BEARISH_WORDS:
            score -= 1.0 * multiplier
    return max(-1.0, min(1.0, score / max(len(words) * 0.1, 1)))


def fetch_news_fmp(symbol: str, api_key: str, limit: int = 50) -> list[dict]:
    try:
        r = requests.get(f"{FMP_BASE}/stock-news", params={
            "tickers": symbol,
            "limit": limit,
            "apikey": api_key,
        }, timeout=15)
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []
    except Exception as exc:
        print(f"  FMP news error ({symbol}): {exc}", file=sys.stderr)
        return []


def fetch_news_general(api_key: str, limit: int = 100) -> list[dict]:
    try:
        r = requests.get(f"{FMP_BASE}/stock-news", params={
            "limit": limit,
            "apikey": api_key,
        }, timeout=15)
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []
    except Exception as exc:
        print(f"  FMP general news error: {exc}", file=sys.stderr)
        return []


def analyze_symbol(symbol: str, api_key: str, days: int) -> dict:
    print(f"  Fetching news for {symbol}...", end=" ", flush=True)
    articles = fetch_news_fmp(symbol, api_key, limit=50)
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    recent = [a for a in articles if a.get("publishedDate", "") >= cutoff]

    if not recent:
        print("no recent articles")
        return {"symbol": symbol, "articles": 0, "sentiment": 0.0, "signal": "neutral"}

    scores = []
    for a in recent:
        title = a.get("title", "")
        text = a.get("text", "") or a.get("summary", "")
        headline_score = score_headline(title)
        body_score = score_headline(text) * 0.5 if text else 0
        scores.append(headline_score * 0.6 + body_score * 0.4)

    avg = sum(scores) / len(scores)
    signal = "strongly_bullish" if avg > 0.3 else (
        "mildly_bullish" if avg > 0.1 else (
            "mildly_bearish" if avg < -0.1 else (
                "strongly_bearish" if avg < -0.3 else "neutral"
            )
        )
    )
    print(f"{len(recent)} articles, sentiment={avg:.3f} ({signal})")
    return {
        "symbol": symbol,
        "articles": len(recent),
        "sentiment": round(avg, 4),
        "signal": signal,
        "top_headlines": [a.get("title", "")[:100] for a in recent[:3]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="News Sentiment Analyzer")
    parser.add_argument("--api-key", help="FMP API key (or set FMP_API_KEY)")
    parser.add_argument("--symbol", help="Single symbol")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--keyword", help="Theme/keyword mode")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("FMP_API_KEY", "")
    if not api_key:
        print("Error: FMP_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    print(f"News Sentiment Analyzer — last {args.days} days")
    print("-" * 50)

    symbols = []
    if args.symbol:
        symbols = [args.symbol.upper()]
    elif args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols = ["AAPL", "MSFT", "NVDA", "TSLA", "META"]

    results = [analyze_symbol(sym, api_key, args.days) for sym in symbols]
    results.sort(key=lambda x: x["sentiment"], reverse=True)

    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": ts,
        "days": args.days,
        "symbols": symbols,
    }

    json_path = str(Path(args.output_dir) / f"sentiment_{ts}.json")
    md_path = str(Path(args.output_dir) / f"sentiment_{ts}.md")

    with open(json_path, "w") as f:
        json.dump({"metadata": metadata, "results": results}, f, indent=2, default=str)
    print(f"\n  JSON → {json_path}")

    lines = [
        "# News Sentiment Analyzer",
        f"**Generated:** {metadata['generated_at']} | **Lookback:** {args.days} days",
        "",
        "| Symbol | Articles | Sentiment | Signal |",
        "|--------|----------|-----------|--------|",
    ]
    for r in results:
        bar = "▲" * int(max(r["sentiment"] * 5, 0)) if r["sentiment"] > 0 else "▼" * int(abs(r["sentiment"]) * 5)
        lines.append(f"| {r['symbol']} | {r['articles']} | {r['sentiment']:+.3f} {bar} | {r['signal']} |")

    with open(md_path, "w") as f:
        f.write("\n".join(lines))
    print(f"  Markdown → {md_path}")
    print(f"\nDone — {len(results)} symbols analyzed.")


if __name__ == "__main__":
    main()
