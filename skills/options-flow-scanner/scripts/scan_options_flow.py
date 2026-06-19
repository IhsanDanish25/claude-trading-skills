#!/usr/bin/env python3
"""Options flow scanner — flags unusual options activity by volume/OI ratio."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests


FMP_BASE = "https://financialmodelingprep.com/stable"
DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN",
    "TSLA", "NFLX", "CRM", "ADBE", "PANW", "CRWD",
]


def _get(endpoint: str, params: dict) -> list | dict | None:
    try:
        r = requests.get(f"{FMP_BASE}/{endpoint}", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data if data else None
    except Exception as exc:
        print(f"  API error ({endpoint}): {exc}", file=sys.stderr)
        return None


def fetch_options_chain(symbol: str, api_key: str) -> list[dict]:
    data = _get(f"options-chain/{symbol}", {"apikey": api_key})
    if not data or not isinstance(data, list):
        return []
    return data


def score_contract(c: dict) -> dict | None:
    volume = c.get("volume") or 0
    oi = c.get("openInterest") or 1
    iv = c.get("impliedVolatility") or 0
    option_type = c.get("optionType", "").upper()
    dte = c.get("daysToExpiration") or 0

    if volume < 100:
        return None

    oi_ratio = volume / max(oi, 1)
    if oi_ratio < 1.5:
        return None

    score = 0
    if oi_ratio >= 5.0:
        score += 40
    elif oi_ratio >= 3.0:
        score += 25
    elif oi_ratio >= 1.5:
        score += 10

    if volume >= 10000:
        score += 30
    elif volume >= 2000:
        score += 15
    elif volume >= 500:
        score += 5

    if 7 <= dte <= 45:
        score += 20
    elif dte < 7:
        score += 5

    if iv > 0.5:
        score += 10

    return {
        "symbol": c.get("symbol", ""),
        "option_type": option_type,
        "strike": c.get("strike"),
        "expiry": c.get("expirationDate", ""),
        "dte": dte,
        "volume": volume,
        "open_interest": oi,
        "oi_ratio": round(oi_ratio, 2),
        "iv": round(float(iv), 4),
        "last_price": c.get("lastPrice"),
        "score": score,
        "signal": "CALL_SWEEP" if option_type == "CALL" else "PUT_SWEEP",
    }


def scan(symbols: list[str], api_key: str, min_volume: int, min_oi_ratio: float) -> list[dict]:
    results = []
    for sym in symbols:
        print(f"  Scanning {sym}...", end=" ", flush=True)
        chain = fetch_options_chain(sym, api_key)
        if not chain:
            print("no data")
            continue
        flagged = 0
        for c in chain:
            scored = score_contract(c)
            if scored and scored["oi_ratio"] >= min_oi_ratio and scored["volume"] >= min_volume:
                results.append(scored)
                flagged += 1
        print(f"{flagged} unusual contracts")
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def write_json(results: list[dict], metadata: dict, path: str) -> None:
    with open(path, "w") as f:
        json.dump({"metadata": metadata, "results": results}, f, indent=2, default=str)
    print(f"  JSON → {path}")


def write_markdown(results: list[dict], metadata: dict, path: str) -> None:
    lines = [
        "# Options Flow Scanner Report",
        f"**Generated:** {metadata['generated_at']}",
        f"**Symbols scanned:** {metadata['symbols_scanned']}",
        f"**Unusual contracts found:** {len(results)}",
        "",
        "---",
        "",
        "## Unusual Options Activity",
        "",
        "| Symbol | Type | Strike | Expiry | DTE | Volume | OI | Vol/OI | IV | Score |",
        "|--------|------|--------|--------|-----|--------|-----|--------|-----|-------|",
    ]
    for r in results[:30]:
        lines.append(
            f"| {r['symbol']} | {r['option_type']} | ${r['strike']} | {r['expiry']} "
            f"| {r['dte']} | {r['volume']:,} | {r['open_interest']:,} "
            f"| {r['oi_ratio']}x | {r['iv']:.1%} | {r['score']} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("*High Vol/OI ratio signals unusual positioning vs. existing open interest.*")
    lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    print(f"  Markdown → {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Options Flow Scanner")
    parser.add_argument("--api-key", help="FMP API key (or set FMP_API_KEY)")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--min-volume", type=int, default=100)
    parser.add_argument("--min-oi-ratio", type=float, default=1.5)
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("FMP_API_KEY", "")
    if not api_key:
        print("Error: FMP_API_KEY not set. Pass --api-key or set env var.", file=sys.stderr)
        sys.exit(1)

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    print(f"Options Flow Scanner — {len(args.symbols)} symbols")
    print("-" * 50)

    results = scan(args.symbols, api_key, args.min_volume, args.min_oi_ratio)

    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": ts,
        "symbols_scanned": len(args.symbols),
        "min_volume": args.min_volume,
        "min_oi_ratio": args.min_oi_ratio,
    }

    json_path = str(Path(args.output_dir) / f"options_flow_{ts}.json")
    md_path = str(Path(args.output_dir) / f"options_flow_{ts}.md")
    write_json(results, metadata, json_path)
    write_markdown(results, metadata, md_path)

    print(f"\nDone — {len(results)} unusual contracts across {len(args.symbols)} symbols.")


if __name__ == "__main__":
    main()
