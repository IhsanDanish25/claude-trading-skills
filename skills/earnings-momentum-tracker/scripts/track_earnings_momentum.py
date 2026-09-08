#!/usr/bin/env python3
"""Earnings momentum tracker — finds post-earnings PEAD continuation plays via yfinance.

Scans a fixed watchlist (like the other yfinance-based dashboard skills) rather
than a market-wide earnings calendar -- yfinance has no bulk "who reported this
week" endpoint, only per-ticker earnings_dates.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("Error: yfinance not installed.", file=sys.stderr)
    sys.exit(1)

DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN", "TSLA", "NFLX", "CRM",
    "ADBE", "PANW", "CRWD", "SNOW", "DDOG", "MELI", "SQ", "SHOP", "NET", "ZS",
]


def get_latest_earnings(symbol: str, lookback_days: int) -> tuple[str, float] | None:
    """Return (earnings_date, eps_surprise_pct) for symbol's latest reported
    earnings within the lookback window, or None if there isn't one."""
    try:
        ed = yf.Ticker(symbol).earnings_dates
        if ed is None or ed.empty:
            return None
        ed = ed.dropna(subset=["Reported EPS", "Surprise(%)"])
        if ed.empty:
            return None
        cutoff = date.today() - timedelta(days=lookback_days)
        ed = ed[(ed.index.date >= cutoff) & (ed.index.date <= date.today())]
        if ed.empty:
            return None
        row = ed.sort_index(ascending=False).iloc[0]
        return row.name.date().isoformat(), float(row["Surprise(%)"])
    except Exception as exc:
        print(f"  yfinance error ({symbol}): {exc}", file=sys.stderr)
        return None


def get_price_history(symbol: str) -> list[dict]:
    try:
        hist = yf.Ticker(symbol).history(period="6mo")
        if hist is None or hist.empty:
            return []
        return [
            {"date": idx.strftime("%Y-%m-%d"), "close": float(row["Close"])}
            for idx, row in hist.iterrows()
        ]
    except Exception as exc:
        print(f"  yfinance error ({symbol}): {exc}", file=sys.stderr)
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


def analyze_stock(symbol: str, earnings_date: str, eps_surprise: float) -> dict | None:
    prices = get_price_history(symbol)
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
    parser = argparse.ArgumentParser(description="Earnings Momentum Tracker (yfinance)")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--min-gap-pct", type=float, default=3.0)
    parser.add_argument("--min-momentum-5d", type=float, default=0.0)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    print(f"Earnings Momentum Tracker — {len(args.symbols)} symbols, last {args.lookback_days} days")
    print("-" * 50)

    results = []
    for sym in args.symbols:
        latest = get_latest_earnings(sym, args.lookback_days)
        if latest is None:
            print(f"  {sym}... no recent earnings")
            continue
        earnings_date, surprise_pct = latest

        if surprise_pct < args.min_gap_pct:
            print(f"  {sym}... EPS surprise {surprise_pct:+.1f}% below threshold")
            continue

        print(f"  Analyzing {sym} (EPS surprise: +{surprise_pct:.1f}%)...", end=" ", flush=True)
        result = analyze_stock(sym, earnings_date, surprise_pct)
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
