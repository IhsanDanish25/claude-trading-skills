#!/usr/bin/env python3
"""Short squeeze scanner — FMP short interest + price/volume momentum."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

FMP_BASE = "https://financialmodelingprep.com/stable"

DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN", "TSLA", "NFLX", "CRM",
    "ADBE", "PANW", "CRWD", "SNOW", "DDOG", "MELI", "SQ", "SHOP", "NET", "ZS",
    "CELH", "ENPH", "FSLR", "ON", "SMCI", "AXON", "DUOL", "PINS", "COIN", "ROKU",
    "UBER", "ABNB", "DASH", "RBLX", "SNAP", "TWLO", "MDB", "GTLB", "DOCN", "S",
]


def fetch_short_interest(symbol: str, api_key: str) -> dict | None:
    try:
        r = requests.get(f"{FMP_BASE}/short-interest", params={
            "symbol": symbol,
            "apikey": api_key,
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and data:
            return data
        return None
    except Exception as exc:
        print(f"  FMP short interest error ({symbol}): {exc}", file=sys.stderr)
        return None


def fetch_quote(symbol: str, api_key: str) -> dict | None:
    try:
        r = requests.get(f"{FMP_BASE}/quote/{symbol}", params={"apikey": api_key}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) and data else None)
    except Exception as exc:
        print(f"  FMP quote error ({symbol}): {exc}", file=sys.stderr)
        return None


def score_squeeze(symbol: str, short_data: dict, quote: dict,
                  min_short_float: float, min_dtc: float) -> dict | None:
    short_float = short_data.get("shortPercentOfFloat") or 0
    short_ratio = short_data.get("shortRatio") or short_data.get("daysTocover") or 0

    if isinstance(short_float, str):
        short_float = float(short_float.replace("%", "")) / 100 if "%" in short_float else float(short_float)
    short_float_pct = short_float * 100 if short_float < 1 else short_float

    if short_float_pct < min_short_float:
        return None
    if short_ratio < min_dtc:
        return None

    price = quote.get("price") or quote.get("close") or 0
    change_pct = quote.get("changesPercentage") or quote.get("changePercent") or 0
    volume = quote.get("volume") or 0
    avg_volume = quote.get("avgVolume") or 1
    vol_ratio = volume / avg_volume if avg_volume > 0 else 0

    short_float_score = min(40, short_float_pct * 1.5)
    dtc_score = min(30, short_ratio * 2)
    momentum_score = min(20, max(0, change_pct) * 2)
    volume_score = min(10, (vol_ratio - 1) * 5) if vol_ratio > 1 else 0

    squeeze_score = round(short_float_score + dtc_score + momentum_score + volume_score)

    grade = "A" if squeeze_score >= 70 else (
        "B" if squeeze_score >= 50 else (
            "C" if squeeze_score >= 30 else "D"
        )
    )

    return {
        "symbol": symbol,
        "price": round(float(price), 2),
        "change_pct": round(float(change_pct), 2),
        "short_float_pct": round(short_float_pct, 2),
        "days_to_cover": round(float(short_ratio), 1),
        "volume": int(volume),
        "volume_ratio": round(vol_ratio, 2),
        "squeeze_score": squeeze_score,
        "grade": grade,
        "setup": "SQUEEZE_SETUP" if squeeze_score >= 60 and change_pct > 0 else (
            "BUILDING" if squeeze_score >= 40 else "WATCH"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Short Squeeze Scanner")
    parser.add_argument("--api-key", help="FMP API key (or set FMP_API_KEY)")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--min-short-float", type=float, default=10.0,
                        help="Minimum short interest as % of float")
    parser.add_argument("--min-days-to-cover", type=float, default=3.0)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("FMP_API_KEY", "")
    if not api_key:
        print("Error: FMP_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    print(f"Short Squeeze Scanner — {len(args.symbols)} symbols")
    print(f"  Min short float: {args.min_short_float}% | Min DTC: {args.min_days_to_cover}")
    print("-" * 50)

    results = []
    for sym in args.symbols:
        print(f"  {sym}...", end=" ", flush=True)
        short_data = fetch_short_interest(sym, api_key)
        if not short_data:
            print("no short data")
            continue
        quote = fetch_quote(sym, api_key)
        if not quote:
            print("no quote")
            continue
        r = score_squeeze(sym, short_data, quote, args.min_short_float, args.min_days_to_cover)
        if r:
            results.append(r)
            print(f"short={r['short_float_pct']}% DTC={r['days_to_cover']} score={r['squeeze_score']} {r['setup']}")
        else:
            print("below threshold")

    results.sort(key=lambda x: x["squeeze_score"], reverse=True)
    top = results[: args.top]

    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": ts,
        "min_short_float_pct": args.min_short_float,
        "min_days_to_cover": args.min_days_to_cover,
        "candidates_found": len(results),
    }

    json_path = str(Path(args.output_dir) / f"short_squeeze_{ts}.json")
    md_path = str(Path(args.output_dir) / f"short_squeeze_{ts}.md")

    with open(json_path, "w") as f:
        json.dump({"metadata": metadata, "results": top}, f, indent=2, default=str)

    lines = [
        "# Short Squeeze Scanner",
        f"**Generated:** {metadata['generated_at']} | Short float ≥ {args.min_short_float}% | DTC ≥ {args.min_days_to_cover}",
        f"**Candidates found:** {len(results)}",
        "",
        "| Symbol | Price | Chg% | Short Float | DTC | Vol Ratio | Setup | Score |",
        "|--------|-------|------|-------------|-----|-----------|-------|-------|",
    ]
    for r in top:
        lines.append(
            f"| {r['symbol']} | ${r['price']} | {r['change_pct']:+.1f}% "
            f"| {r['short_float_pct']}% | {r['days_to_cover']} "
            f"| {r['volume_ratio']}x | {r['setup']} | {r['squeeze_score']} |"
        )
    with open(md_path, "w") as f:
        f.write("\n".join(lines))

    print(f"\n  JSON → {json_path}")
    print(f"  Markdown → {md_path}")
    print(f"\nDone — {len(top)} short squeeze candidates.")


if __name__ == "__main__":
    main()
