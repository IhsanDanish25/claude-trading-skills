#!/usr/bin/env python3
"""Breakout scanner — price/volume breakouts above resistance levels."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("Error: yfinance not installed.", file=sys.stderr)
    sys.exit(1)

DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN", "TSLA", "NFLX", "CRM",
    "ADBE", "PANW", "CRWD", "SNOW", "DDOG", "MELI", "SQ", "SHOP", "NET", "ZS",
    "CELH", "ENPH", "FSLR", "ON", "SMCI", "AXON", "DUOL", "PINS", "COIN", "ROKU",
    "UBER", "ABNB", "DASH", "RBLX", "SNAP", "TWLO", "MDB", "GTLB", "DOCN", "S",
]


def fetch(symbol: str) -> list[dict]:
    try:
        hist = yf.Ticker(symbol).history(period="1y")
        if hist.empty:
            return []
        return [
            {"date": str(i.date()), "high": float(r["High"]), "low": float(r["Low"]),
             "close": float(r["Close"]), "volume": int(r["Volume"])}
            for i, r in hist.iterrows()
        ]
    except Exception:
        return []


def detect_breakout(symbol: str, bars: list[dict], lookback: int,
                    min_vol_ratio: float, mode: str) -> dict | None:
    if len(bars) < lookback + 5:
        return None

    recent = bars[-5:]
    historical = bars[-(lookback + 5):-5]

    if not historical or not recent:
        return None

    current_close = recent[-1]["close"]
    current_vol = recent[-1]["volume"]
    avg_vol = sum(b["volume"] for b in bars[-21:-1]) / 20 if len(bars) >= 22 else 1
    vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0

    if vol_ratio < min_vol_ratio:
        return None

    resistance = max(b["high"] for b in historical)
    year_high = max(b["high"] for b in bars[:-1])
    consolidation_range = (max(b["high"] for b in historical[-20:]) -
                           min(b["low"] for b in historical[-20:])) / current_close * 100

    is_52wk_breakout = current_close > year_high * 0.995
    is_box_breakout = current_close > resistance * 0.995

    if mode == "52wk-high" and not is_52wk_breakout:
        return None
    if mode == "box-breakout" and not is_box_breakout:
        return None
    if mode == "any" and not (is_52wk_breakout or is_box_breakout):
        return None

    base_length_weeks = lookback // 5
    score = 0
    score += min(40, vol_ratio / min_vol_ratio * 20)
    score += min(25, base_length_weeks * 2)
    if is_52wk_breakout:
        score += 20
    if consolidation_range < 15:
        score += 15

    breakout_type = []
    if is_52wk_breakout:
        breakout_type.append("52wk-high")
    if is_box_breakout:
        breakout_type.append("box-breakout")

    sma50 = sum(b["close"] for b in bars[-51:-1]) / 50 if len(bars) >= 52 else None
    sma200 = sum(b["close"] for b in bars[-201:-1]) / 200 if len(bars) >= 202 else None

    return {
        "symbol": symbol,
        "price": round(current_close, 2),
        "resistance": round(resistance, 2),
        "year_high": round(year_high, 2),
        "volume": current_vol,
        "volume_ratio": round(vol_ratio, 2),
        "breakout_type": breakout_type,
        "consolidation_weeks": base_length_weeks,
        "range_width_pct": round(consolidation_range, 2),
        "sma_50": round(sma50, 2) if sma50 else None,
        "sma_200": round(sma200, 2) if sma200 else None,
        "above_sma200": current_close > sma200 if sma200 else None,
        "breakout_score": round(score),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Breakout Scanner")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--min-volume-ratio", type=float, default=1.5)
    parser.add_argument("--lookback-days", type=int, default=60)
    parser.add_argument("--mode", choices=["52wk-high", "box-breakout", "any"], default="any")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    print(f"Breakout Scanner — {len(args.symbols)} symbols, mode={args.mode}")
    print("-" * 50)

    results = []
    for sym in args.symbols:
        print(f"  {sym}...", end=" ", flush=True)
        bars = fetch(sym)
        r = detect_breakout(sym, bars, args.lookback_days, args.min_volume_ratio, args.mode)
        if r:
            results.append(r)
            print(f"BREAKOUT vol_ratio={r['volume_ratio']}x score={r['breakout_score']}")
        else:
            print("no breakout")

    results.sort(key=lambda x: x["breakout_score"], reverse=True)
    top = results[: args.top]

    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": ts,
        "mode": args.mode,
        "min_volume_ratio": args.min_volume_ratio,
        "breakouts_found": len(results),
    }

    json_path = str(Path(args.output_dir) / f"breakouts_{ts}.json")
    md_path = str(Path(args.output_dir) / f"breakouts_{ts}.md")

    with open(json_path, "w") as f:
        json.dump({"metadata": metadata, "results": top}, f, indent=2, default=str)

    lines = [
        "# Breakout Scanner",
        f"**Generated:** {metadata['generated_at']} | Mode: {args.mode} | Min Vol Ratio: {args.min_volume_ratio}x",
        f"**Breakouts found:** {len(results)}",
        "",
        "| Symbol | Price | Resistance | Vol Ratio | Type | Base Weeks | Score |",
        "|--------|-------|------------|-----------|------|------------|-------|",
    ]
    for r in top:
        btype = " + ".join(r["breakout_type"]) if r["breakout_type"] else "?"
        lines.append(
            f"| {r['symbol']} | ${r['price']} | ${r['resistance']} "
            f"| {r['volume_ratio']}x | {btype} | {r['consolidation_weeks']}w | {r['breakout_score']} |"
        )
    with open(md_path, "w") as f:
        f.write("\n".join(lines))

    print(f"\n  JSON → {json_path}")
    print(f"  Markdown → {md_path}")
    print(f"\nDone — {len(top)} breakout candidates.")


if __name__ == "__main__":
    main()
