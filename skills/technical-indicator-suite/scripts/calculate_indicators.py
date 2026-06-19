#!/usr/bin/env python3
"""Technical indicator suite — RSI, MACD, Bollinger Bands, ATR, EMA."""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yfinance as yf
except ImportError:
    print("Error: yfinance not installed. Run: pip install yfinance", file=sys.stderr)
    sys.exit(1)


DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMD", "META"]


def fetch_prices(symbol: str, period: str = "6mo") -> list[dict]:
    try:
        hist = yf.Ticker(symbol).history(period=period)
        if hist.empty:
            return []
        return [
            {
                "date": str(idx.date()),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": int(row["Volume"]),
            }
            for idx, row in hist.iterrows()
        ]
    except Exception as exc:
        print(f"  yfinance error ({symbol}): {exc}", file=sys.stderr)
        return []


def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-period:]]
    losses = [abs(min(d, 0)) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calc_ema(closes: list[float], period: int) -> list[float]:
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def calc_macd(closes: list[float], fast: int = 12, slow: int = 26,
              signal: int = 9) -> dict | None:
    if len(closes) < slow + signal:
        return None
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    if not ema_fast or not ema_slow:
        return None
    min_len = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[-(min_len - i)] - ema_slow[-(min_len - i)] for i in range(min_len)]
    signal_line = calc_ema(macd_line, signal)
    if not signal_line:
        return None
    hist_val = macd_line[-1] - signal_line[-1]
    return {
        "macd": round(macd_line[-1], 4),
        "signal": round(signal_line[-1], 4),
        "histogram": round(hist_val, 4),
        "crossover": "bullish" if macd_line[-1] > signal_line[-1] else "bearish",
    }


def calc_bb(closes: list[float], period: int = 20, std_dev: float = 2.0) -> dict | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = mean + std_dev * std
    lower = mean - std_dev * std
    current = closes[-1]
    bandwidth = (upper - lower) / mean * 100
    pct_b = (current - lower) / (upper - lower) if upper != lower else 0.5
    return {
        "upper": round(upper, 2),
        "middle": round(mean, 2),
        "lower": round(lower, 2),
        "bandwidth_pct": round(bandwidth, 2),
        "pct_b": round(pct_b, 4),
        "position": "above_upper" if current > upper else ("below_lower" if current < lower else "inside"),
    }


def calc_atr(bars: list[dict], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        high = bars[i]["high"]
        low = bars[i]["low"]
        prev_close = bars[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 4)


def signal_summary(rsi: float | None, macd: dict | None, bb: dict | None) -> str:
    signals = []
    if rsi is not None:
        if rsi < 30:
            signals.append("RSI_OVERSOLD")
        elif rsi > 70:
            signals.append("RSI_OVERBOUGHT")
    if macd and macd["crossover"] == "bullish":
        signals.append("MACD_BULLISH")
    elif macd and macd["crossover"] == "bearish":
        signals.append("MACD_BEARISH")
    if bb:
        if bb["position"] == "below_lower":
            signals.append("BB_OVERSOLD")
        elif bb["position"] == "above_upper":
            signals.append("BB_OVERBOUGHT")
        if bb["bandwidth_pct"] < 5:
            signals.append("BB_SQUEEZE")
    if not signals:
        return "NEUTRAL"
    return " | ".join(signals)


def analyze(symbol: str, rsi_oversold: float, rsi_overbought: float) -> dict | None:
    bars = fetch_prices(symbol)
    if len(bars) < 30:
        return None
    closes = [b["close"] for b in bars]
    rsi = calc_rsi(closes)
    macd = calc_macd(closes)
    bb = calc_bb(closes)
    atr = calc_atr(bars)
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)
    current = closes[-1]
    return {
        "symbol": symbol,
        "price": round(current, 2),
        "rsi_14": rsi,
        "rsi_signal": "oversold" if rsi and rsi < rsi_oversold else ("overbought" if rsi and rsi > rsi_overbought else "neutral"),
        "macd": macd,
        "bollinger_bands": bb,
        "atr_14": atr,
        "ema_50": round(ema50[-1], 2) if ema50 else None,
        "ema_200": round(ema200[-1], 2) if ema200 else None,
        "above_ema50": current > ema50[-1] if ema50 else None,
        "above_ema200": current > ema200[-1] if ema200 else None,
        "signal_summary": signal_summary(rsi, macd, bb),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Technical Indicator Suite")
    parser.add_argument("--symbol", help="Single symbol")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--rsi-period", type=int, default=14)
    parser.add_argument("--rsi-oversold", type=float, default=30.0)
    parser.add_argument("--rsi-overbought", type=float, default=70.0)
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    symbols = [args.symbol.upper()] if args.symbol else [s.upper() for s in args.symbols]
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    print(f"Technical Indicator Suite — {len(symbols)} symbols")
    print("-" * 50)

    results = []
    for sym in symbols:
        print(f"  Calculating {sym}...", end=" ", flush=True)
        r = analyze(sym, args.rsi_oversold, args.rsi_overbought)
        if r:
            results.append(r)
            print(f"RSI={r['rsi_14']} MACD={r['macd']['crossover'] if r['macd'] else 'N/A'} {r['signal_summary']}")
        else:
            print("no data")

    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": ts,
        "symbols": symbols,
        "rsi_oversold": args.rsi_oversold,
        "rsi_overbought": args.rsi_overbought,
    }

    json_path = str(Path(args.output_dir) / f"indicators_{ts}.json")
    md_path = str(Path(args.output_dir) / f"indicators_{ts}.md")

    with open(json_path, "w") as f:
        json.dump({"metadata": metadata, "results": results}, f, indent=2, default=str)
    print(f"\n  JSON → {json_path}")

    lines = [
        "# Technical Indicator Suite",
        f"**Generated:** {metadata['generated_at']}",
        "",
        "| Symbol | Price | RSI | MACD | BB Position | ATR | EMA50 | Signal |",
        "|--------|-------|-----|------|-------------|-----|-------|--------|",
    ]
    for r in results:
        macd_str = r["macd"]["crossover"] if r["macd"] else "N/A"
        bb_pos = r["bollinger_bands"]["position"] if r["bollinger_bands"] else "N/A"
        lines.append(
            f"| {r['symbol']} | ${r['price']} | {r['rsi_14']} | {macd_str} "
            f"| {bb_pos} | {r['atr_14']} | ${r['ema_50']} | {r['signal_summary']} |"
        )
    with open(md_path, "w") as f:
        f.write("\n".join(lines))
    print(f"  Markdown → {md_path}")
    print(f"\nDone — {len(results)} symbols analyzed.")


if __name__ == "__main__":
    main()
