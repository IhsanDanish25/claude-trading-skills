#!/usr/bin/env python3
"""Sector rotation detector — ranks 11 SPDR ETFs by multi-timeframe momentum."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("Error: yfinance not installed. Run: pip install yfinance", file=sys.stderr)
    sys.exit(1)


SECTORS = {
    "XLK": "Technology",
    "XLV": "Health Care",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLB": "Materials",
    "XLC": "Communication Services",
}


def momentum(prices: list[float], window: int) -> float | None:
    if len(prices) < window + 1:
        return None
    current = prices[-1]
    past = prices[-(window + 1)]
    if past <= 0:
        return None
    return round((current - past) / past * 100, 2)


def fetch_etf_data(ticker: str, period: str = "6mo") -> list[float]:
    try:
        hist = yf.Ticker(ticker).history(period=period)
        return hist["Close"].tolist() if not hist.empty else []
    except Exception as exc:
        print(f"  yfinance error ({ticker}): {exc}", file=sys.stderr)
        return []


def classify_rotation(results: list[dict]) -> str:
    sorted_r = sorted(results, key=lambda x: x.get("momentum_1m") or -999, reverse=True)
    top3 = [r["ticker"] for r in sorted_r[:3]]
    if "XLK" in top3 or "XLC" in top3:
        return "Growth/Tech Leading — risk-on momentum regime"
    if "XLF" in top3 or "XLI" in top3 or "XLY" in top3:
        return "Cyclicals Leading — early/mid expansion"
    if "XLP" in top3 or "XLU" in top3 or "XLV" in top3:
        return "Defensives Leading — late cycle or risk-off rotation"
    if "XLE" in top3 or "XLB" in top3:
        return "Commodities/Materials Leading — inflation or supply-shock regime"
    return "Mixed — no clear rotation signal"


def main() -> None:
    parser = argparse.ArgumentParser(description="Sector Rotation Detector")
    parser.add_argument("--benchmark", default="SPY", help="Benchmark ticker for RS calculation")
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    print("Sector Rotation Detector — SPDR ETFs")
    print("-" * 50)

    benchmark_prices = fetch_etf_data(args.benchmark, "6mo")
    bench_1m = momentum(benchmark_prices, 21)
    bench_3m = momentum(benchmark_prices, 63)
    bench_6m = momentum(benchmark_prices, 126)

    results = []
    for ticker, name in SECTORS.items():
        print(f"  Fetching {ticker} ({name})...", end=" ", flush=True)
        prices = fetch_etf_data(ticker, "7mo")
        if not prices:
            print("no data")
            continue

        m1 = momentum(prices, 21)
        m3 = momentum(prices, 63)
        m6 = momentum(prices, 126)

        rs_1m = round(m1 - (bench_1m or 0), 2) if m1 is not None and bench_1m is not None else None
        rs_3m = round(m3 - (bench_3m or 0), 2) if m3 is not None and bench_3m is not None else None

        composite = 0.0
        weight = 0
        if m1 is not None:
            composite += m1 * 0.5
            weight += 0.5
        if m3 is not None:
            composite += m3 * 0.3
            weight += 0.3
        if m6 is not None:
            composite += m6 * 0.2
            weight += 0.2
        composite_score = round(composite / weight, 2) if weight > 0 else None

        results.append({
            "ticker": ticker,
            "name": name,
            "momentum_1m": m1,
            "momentum_3m": m3,
            "momentum_6m": m6,
            "rs_vs_spy_1m": rs_1m,
            "rs_vs_spy_3m": rs_3m,
            "composite_score": composite_score,
        })
        print(f"1m={m1}% 3m={m3}% 6m={m6}%")

    results.sort(key=lambda x: x.get("composite_score") or -999, reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1

    rotation_signal = classify_rotation(results)
    print(f"\nRotation signal: {rotation_signal}")

    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": ts,
        "benchmark": args.benchmark,
        "rotation_signal": rotation_signal,
    }

    json_path = str(Path(args.output_dir) / f"sector_rotation_{ts}.json")
    md_path = str(Path(args.output_dir) / f"sector_rotation_{ts}.md")

    with open(json_path, "w") as f:
        json.dump({"metadata": metadata, "results": results}, f, indent=2, default=str)
    print(f"  JSON → {json_path}")

    lines = [
        "# Sector Rotation Detector",
        f"**Generated:** {metadata['generated_at']}",
        f"**Rotation Signal:** {rotation_signal}",
        "",
        "| Rank | ETF | Sector | 1M% | 3M% | 6M% | RS vs SPY 1M | Composite |",
        "|------|-----|--------|-----|-----|-----|--------------|-----------|",
    ]
    for r in results:
        rs_str = f"{r['rs_vs_spy_1m']:+.1f}%" if r["rs_vs_spy_1m"] is not None else "N/A"
        lines.append(
            f"| {r['rank']} | {r['ticker']} | {r['name']} "
            f"| {r.get('momentum_1m', 'N/A')}% | {r.get('momentum_3m', 'N/A')}% "
            f"| {r.get('momentum_6m', 'N/A')}% | {rs_str} "
            f"| {r.get('composite_score', 'N/A')} |"
        )
    lines.extend(["", f"**Leading:** {', '.join(r['ticker'] for r in results[:3])}",
                  f"**Lagging:** {', '.join(r['ticker'] for r in results[-3:])}"])
    with open(md_path, "w") as f:
        f.write("\n".join(lines))
    print(f"  Markdown → {md_path}")
    print(f"\nDone — {len(results)} sectors ranked.")


if __name__ == "__main__":
    main()
