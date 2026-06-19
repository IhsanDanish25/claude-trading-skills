#!/usr/bin/env python3
"""Mean reversion screener — RSI + Bollinger Band oversold in uptrending stocks."""
from __future__ import annotations

import argparse
import json
import math
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
]


def fetch(symbol: str) -> list[dict]:
    try:
        hist = yf.Ticker(symbol).history(period="1y")
        if hist.empty:
            return []
        return [
            {"date": str(i.date()), "open": float(r["Open"]), "high": float(r["High"]),
             "low": float(r["Low"]), "close": float(r["Close"]), "volume": int(r["Volume"])}
            for i, r in hist.iterrows()
        ]
    except Exception:
        return []


def sma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-period:]]
    losses = [abs(min(d, 0)) for d in deltas[-period:]]
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag / al)), 2)


def bollinger(closes: list[float], period: int = 20, k: float = 2.0) -> dict | None:
    if len(closes) < period:
        return None
    w = closes[-period:]
    mean = sum(w) / period
    std = math.sqrt(sum((x - mean) ** 2 for x in w) / period)
    return {
        "upper": round(mean + k * std, 2),
        "middle": round(mean, 2),
        "lower": round(mean - k * std, 2),
        "pct_b": round((closes[-1] - (mean - k * std)) / (2 * k * std), 4) if std > 0 else 0.5,
    }


def volume_trend(bars: list[dict], n: int = 10) -> str:
    if len(bars) < n + 1:
        return "unknown"
    recent = bars[-n:]
    down_bars = [b for b in recent if b["close"] < b["open"]]
    down_vol = sum(b["volume"] for b in down_bars)
    avg_vol = sum(b["volume"] for b in recent) / len(recent)
    avg_down = down_vol / max(len(down_bars), 1)
    return "healthy" if avg_down < avg_vol else "distribution"


def analyze(symbol: str, rsi_max: float, min_pullback: float, max_distance_50d: float) -> dict | None:
    bars = fetch(symbol)
    if len(bars) < 50:
        return None
    closes = [b["close"] for b in bars]
    price = closes[-1]

    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    if not sma50 or not sma200:
        return None
    if price < sma200:
        return None

    rsi_val = rsi(closes)
    if rsi_val is None or rsi_val > rsi_max:
        return None

    bb = bollinger(closes)
    pullback = (sma50 - price) / sma50 * 100 if sma50 > price else 0

    if pullback < min_pullback and (bb is None or bb["pct_b"] > 0.2):
        return None

    dist_50d = abs(price - sma50) / sma50 * 100
    if dist_50d > max_distance_50d and rsi_val > 35:
        return None

    vol_trend = volume_trend(bars)

    rsi_score = max(0, (rsi_max - rsi_val) / rsi_max * 35)
    bb_score = max(0, (0.5 - (bb["pct_b"] if bb else 0.5)) * 30 * 2) if bb else 0
    pullback_score = min(20, pullback * 2)
    vol_score = 15 if vol_trend == "healthy" else 0
    score = round(rsi_score + bb_score + pullback_score + vol_score)

    return {
        "symbol": symbol,
        "price": round(price, 2),
        "sma_50": round(sma50, 2),
        "sma_200": round(sma200, 2),
        "above_sma200": True,
        "rsi_14": rsi_val,
        "bb_lower": bb["lower"] if bb else None,
        "bb_pct_b": bb["pct_b"] if bb else None,
        "pullback_pct": round(pullback, 2),
        "volume_trend": vol_trend,
        "reversion_score": score,
        "target": round(sma50, 2),
        "risk_reward": round(pullback / max(abs(price - (bb["lower"] if bb else price) * 0.97), 0.01), 1) if bb else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Mean Reversion Screener")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--rsi-max", type=float, default=40.0)
    parser.add_argument("--min-pullback-pct", type=float, default=5.0)
    parser.add_argument("--max-distance-from-50d", type=float, default=20.0)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    print(f"Mean Reversion Screener — {len(args.symbols)} symbols")
    print("-" * 50)

    results = []
    for sym in args.symbols:
        print(f"  {sym}...", end=" ", flush=True)
        r = analyze(sym, args.rsi_max, args.min_pullback_pct, args.max_distance_from_50d)
        if r:
            results.append(r)
            print(f"RSI={r['rsi_14']} pullback={r['pullback_pct']}% score={r['reversion_score']}")
        else:
            print("skip")

    results.sort(key=lambda x: x["reversion_score"], reverse=True)
    top = results[: args.top]

    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": ts,
        "rsi_max": args.rsi_max,
        "min_pullback_pct": args.min_pullback_pct,
        "candidates": len(results),
    }

    json_path = str(Path(args.output_dir) / f"mean_reversion_{ts}.json")
    md_path = str(Path(args.output_dir) / f"mean_reversion_{ts}.md")

    with open(json_path, "w") as f:
        json.dump({"metadata": metadata, "results": top}, f, indent=2, default=str)

    lines = [
        "# Mean Reversion Screener",
        f"**Generated:** {metadata['generated_at']} | RSI max: {args.rsi_max}",
        "",
        "| Symbol | Price | RSI | Pullback% | BB%b | Vol Trend | Target | Score |",
        "|--------|-------|-----|-----------|------|-----------|--------|-------|",
    ]
    for r in top:
        pct_b = f"{r['bb_pct_b']:.2f}" if r["bb_pct_b"] is not None else "N/A"
        lines.append(
            f"| {r['symbol']} | ${r['price']} | {r['rsi_14']} | {r['pullback_pct']}% "
            f"| {pct_b} | {r['volume_trend']} | ${r['target']} | {r['reversion_score']} |"
        )
    with open(json_path, "w") as f:
        json.dump({"metadata": metadata, "results": top}, f, indent=2, default=str)
    with open(md_path, "w") as f:
        f.write("\n".join(lines))

    print(f"\n  JSON → {json_path}")
    print(f"  Markdown → {md_path}")
    print(f"\nDone — {len(top)} mean reversion candidates.")


if __name__ == "__main__":
    main()
