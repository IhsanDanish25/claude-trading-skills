#!/usr/bin/env python3
"""Earnings momentum tracker — finds post-earnings PEAD continuation plays."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests


FMP_BASE = "https://financialmodelingprep.com/stable"


def _get(endpoint: str, params: dict) -> Any:
    try:
        r = requests.get(f"{FMP_BASE}/{endpoint}", params=params, timeout=15)
        r.raise_for_status()
        return r.json() or None
    except Exception as exc:
        print(f"  API error ({endpoint}): {exc}", file=sys.stderr)
        return None


def get_recent_earnings(api_key: str, from_date: str, to_date: str) -> list[dict]:
    data = _get("earning_calendar", {
        "from": from_date,
        "to": to_date,
        "apikey": api_key,
    })
    return data if isinstance(data, list) else []


def get_price_history(symbol: str, api_key: str, days: int = 30) -> list[dict]:
    data = _get(f"historical-price-eod/{symbol}", {
        "timeseries": days,
        "apikey": api_key,
    })
    if isinstance(data, dict):
        return data.get("historical", [])
    return []


def calc_momentum(prices: list[dict], earnings_date: str, window: int) -> float | None:
    sorted_prices = sorted(prices, key=lambda x: x.get("date", ""))
    earn_idx = None
    for i, p in enumerate(sorted_prices):
        if p.get("date", "") >= earnings_date:
            earn_idx = i
            break
    if earn_idx is None or earn_idx + window >= len(sorted_prices):
        return None
    start_price = sorted_prices[earn_idx].get("close", 0)
    end_price = sorted_prices[min(earn_idx + window, len(sorted_prices) - 1)].get("close", 0)
    if start_price <= 0:
        return None
    return round((end_price - start_price) / start_price * 100, 2)


def grade_momentum(momentum_20d: float | None) -> str:
    if momentum_20d is None:
        return "?"
    if momentum_20d >= 15:
        return "A"
    if momentum_20d >= 8:
        return "B"
    if momentum_20d >= 3:
        return "C"
    return "D"


def analyze_stock(symbol: str, earnings_date: str, eps_surprise: float,
                  api_key: str) -> dict | None:
    prices = get_price_history(symbol, api_key, days=35)
    if len(prices) < 10:
        return None

    m5 = calc_momentum(prices, earnings_date, 5)
    m10 = calc_momentum(prices, earnings_date, 10)
    m20 = calc_momentum(prices, earnings_date, 20)

    if m5 is None:
        return None

    grade = grade_momentum(m20)
    score = 0
    if m5 is not None and m5 > 0:
        score += 30
    if m10 is not None and m10 > m5:
        score += 20
    if m20 is not None and m20 > 5:
        score += 30
    if eps_surprise > 10:
        score += 20
    elif eps_surprise > 5:
        score += 10

    recent_prices = sorted(prices, key=lambda x: x.get("date", ""), reverse=True)
    current_price = recent_prices[0].get("close", 0) if recent_prices else 0

    return {
        "symbol": symbol,
        "earnings_date": earnings_date,
        "eps_surprise_pct": round(eps_surprise, 2),
        "current_price": current_price,
        "momentum_5d": m5,
        "momentum_10d": m10,
        "momentum_20d": m20,
        "grade": grade,
        "score": score,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Earnings Momentum Tracker")
    parser.add_argument("--api-key", help="FMP API key (or set FMP_API_KEY)")
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--min-gap-pct", type=float, default=3.0)
    parser.add_argument("--min-momentum-5d", type=float, default=0.0)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("FMP_API_KEY", "")
    if not api_key:
        print("Error: FMP_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    from_date = (date.today() - timedelta(days=args.lookback_days)).isoformat()
    to_date = date.today().isoformat()

    print(f"Earnings Momentum Tracker — last {args.lookback_days} days")
    print("-" * 50)
    print("Fetching earnings calendar...")

    earnings = get_recent_earnings(api_key, from_date, to_date)
    print(f"  {len(earnings)} earnings events found")

    results = []
    seen = set()
    for e in earnings:
        sym = e.get("symbol", "")
        eps_surprise = float(e.get("epsEstimated") or 0)
        actual_eps = float(e.get("eps") or 0)
        surprise_pct = 0.0
        if eps_surprise != 0:
            surprise_pct = (actual_eps - eps_surprise) / abs(eps_surprise) * 100

        if sym in seen or not sym:
            continue
        seen.add(sym)

        if surprise_pct < args.min_gap_pct:
            continue

        print(f"  Analyzing {sym} (EPS surprise: +{surprise_pct:.1f}%)...", end=" ", flush=True)
        result = analyze_stock(sym, e.get("date", ""), surprise_pct, api_key)
        if result and (result["momentum_5d"] or 0) >= args.min_momentum_5d:
            results.append(result)
            print(f"Grade {result['grade']}, 20d={result.get('momentum_20d', '?')}%")
        else:
            print("skip")

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[: args.top]

    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": ts,
        "lookback_days": args.lookback_days,
        "min_gap_pct": args.min_gap_pct,
        "candidates_found": len(results),
    }

    json_path = str(Path(args.output_dir) / f"earnings_momentum_{ts}.json")
    md_path = str(Path(args.output_dir) / f"earnings_momentum_{ts}.md")

    with open(json_path, "w") as f:
        json.dump({"metadata": metadata, "results": top}, f, indent=2, default=str)
    print(f"\n  JSON → {json_path}")

    lines = [
        "# Earnings Momentum Tracker",
        f"**Generated:** {metadata['generated_at']}",
        f"**Lookback:** {args.lookback_days} days | **Candidates:** {len(results)}",
        "",
        "| Symbol | Earnings | EPS Surp% | Price | 5d% | 10d% | 20d% | Grade | Score |",
        "|--------|----------|-----------|-------|-----|------|------|-------|-------|",
    ]
    for r in top:
        lines.append(
            f"| {r['symbol']} | {r['earnings_date']} | +{r['eps_surprise_pct']:.1f}% "
            f"| ${r['current_price']:.2f} | {r.get('momentum_5d', '?')}% "
            f"| {r.get('momentum_10d', '?')}% | {r.get('momentum_20d', '?')}% "
            f"| {r['grade']} | {r['score']} |"
        )
    with open(md_path, "w") as f:
        f.write("\n".join(lines))
    print(f"  Markdown → {md_path}")
    print(f"\nDone — {len(top)} PEAD candidates.")


if __name__ == "__main__":
    main()
