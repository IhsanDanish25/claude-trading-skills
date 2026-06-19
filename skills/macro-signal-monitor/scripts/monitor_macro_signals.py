#!/usr/bin/env python3
"""Macro signal monitor — yield curve, credit spreads, cross-asset regime indicators."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("Error: yfinance not installed.", file=sys.stderr)
    sys.exit(1)

MACRO_ASSETS = {
    "^TNX": "10Y Treasury Yield",
    "^IRX": "3M T-Bill Yield",
    "^TYX": "30Y Treasury Yield",
    "HYG": "High-Yield Bond ETF (Credit Spreads Proxy)",
    "LQD": "Investment Grade Bond ETF",
    "GLD": "Gold",
    "DX-Y.NYB": "US Dollar Index (DXY)",
    "CL=F": "Crude Oil (WTI)",
    "^VIX": "VIX Volatility",
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "IEF": "7-10Y Treasury Bond ETF",
    "TLT": "20Y+ Treasury Bond ETF",
}

RISK_INDICATORS = {
    "HYG": "risk_on",
    "GLD": "risk_off_hedge",
    "^VIX": "fear_gauge",
    "DX-Y.NYB": "dollar_strength",
    "CL=F": "growth_proxy",
}


def fetch_price_series(ticker: str, period: str = "3mo") -> list[float]:
    try:
        hist = yf.Ticker(ticker).history(period=period)
        return hist["Close"].tolist() if not hist.empty else []
    except Exception as exc:
        print(f"  yfinance error ({ticker}): {exc}", file=sys.stderr)
        return []


def momentum(prices: list[float], window: int) -> float | None:
    if len(prices) < window + 1:
        return None
    current = prices[-1]
    past = prices[-(window + 1)]
    return round((current - past) / past * 100, 2) if past != 0 else None


def classify_yield_curve(short_yield: float | None, long_yield: float | None) -> dict:
    if short_yield is None or long_yield is None:
        return {"spread": None, "shape": "unknown", "signal": "insufficient data"}
    spread = long_yield - short_yield
    shape = "normal" if spread > 0.5 else ("flat" if spread > -0.1 else "inverted")
    signal = (
        "healthy expansion" if shape == "normal" else
        ("late cycle caution" if shape == "flat" else "recession risk elevated")
    )
    return {"spread": round(spread, 3), "shape": shape, "signal": signal}


def assess_regime(signals: dict) -> str:
    risk_on_score = 0
    risk_off_score = 0

    vix = signals.get("^VIX", {}).get("current")
    if vix is not None:
        if vix < 15:
            risk_on_score += 2
        elif vix > 25:
            risk_off_score += 2

    hyg_mom = signals.get("HYG", {}).get("momentum_1m")
    if hyg_mom is not None:
        if hyg_mom > 1:
            risk_on_score += 2
        elif hyg_mom < -1:
            risk_off_score += 2

    spy_mom = signals.get("SPY", {}).get("momentum_1m")
    if spy_mom is not None:
        if spy_mom > 2:
            risk_on_score += 1
        elif spy_mom < -2:
            risk_off_score += 1

    gld_mom = signals.get("GLD", {}).get("momentum_1m")
    if gld_mom is not None:
        if gld_mom > 2:
            risk_off_score += 1

    if risk_on_score > risk_off_score + 2:
        return "RISK-ON — equities and credit favored"
    if risk_off_score > risk_on_score + 2:
        return "RISK-OFF — defensive rotation, volatility elevated"
    return "NEUTRAL — mixed signals, reduce sizing"


def main() -> None:
    parser = argparse.ArgumentParser(description="Macro Signal Monitor")
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    print("Macro Signal Monitor — Cross-Asset Regime Dashboard")
    print("-" * 55)

    signals = {}
    for ticker, name in MACRO_ASSETS.items():
        print(f"  Fetching {ticker} ({name})...", end=" ", flush=True)
        prices = fetch_price_series(ticker, "6mo")
        if not prices:
            print("no data")
            continue
        m1 = momentum(prices, 21)
        m3 = momentum(prices, 63)
        current = round(prices[-1], 4)
        signals[ticker] = {
            "name": name,
            "current": current,
            "momentum_1m": m1,
            "momentum_3m": m3,
        }
        print(f"${current} 1m={m1}% 3m={m3}%")

    tnx_val = signals.get("^TNX", {}).get("current")
    irx_val = signals.get("^IRX", {}).get("current")
    yield_curve = classify_yield_curve(irx_val, tnx_val)
    regime = assess_regime(signals)

    print(f"\nYield curve: {yield_curve['shape']} (spread={yield_curve['spread']}%)")
    print(f"Regime: {regime}")

    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": ts,
        "yield_curve": yield_curve,
        "regime": regime,
    }

    json_path = str(Path(args.output_dir) / f"macro_signals_{ts}.json")
    md_path = str(Path(args.output_dir) / f"macro_signals_{ts}.md")

    with open(json_path, "w") as f:
        json.dump({"metadata": metadata, "signals": signals}, f, indent=2, default=str)

    lines = [
        "# Macro Signal Monitor",
        f"**Generated:** {metadata['generated_at']}",
        "",
        f"## Regime: {regime}",
        f"**Yield Curve:** {yield_curve['shape'].upper()} | 10Y-3M Spread: {yield_curve.get('spread', 'N/A')}% | {yield_curve['signal']}",
        "",
        "## Cross-Asset Dashboard",
        "",
        "| Asset | Current | 1M Momentum | 3M Momentum | Signal |",
        "|-------|---------|-------------|-------------|--------|",
    ]
    for ticker, data in signals.items():
        m1 = data["momentum_1m"]
        m3 = data["momentum_3m"]
        signal = ""
        if ticker == "^VIX":
            signal = "HIGH FEAR" if (data["current"] or 0) > 25 else ("LOW FEAR" if (data["current"] or 0) < 15 else "NORMAL")
        elif m1 is not None:
            signal = "BULLISH" if m1 > 2 else ("BEARISH" if m1 < -2 else "NEUTRAL")
        lines.append(
            f"| {ticker} | {data['current']} | {m1}% | {m3}% | {signal} |"
        )

    lines.extend([
        "",
        "## Key Risk Indicators",
        f"- **VIX:** {signals.get('^VIX', {}).get('current', 'N/A')}",
        f"- **10Y Yield:** {signals.get('^TNX', {}).get('current', 'N/A')}%",
        f"- **3M Yield:** {signals.get('^IRX', {}).get('current', 'N/A')}%",
        f"- **DXY:** {signals.get('DX-Y.NYB', {}).get('current', 'N/A')}",
        f"- **Gold:** ${signals.get('GLD', {}).get('current', 'N/A')}",
        f"- **Oil (WTI):** ${signals.get('CL=F', {}).get('current', 'N/A')}",
    ])

    with open(md_path, "w") as f:
        f.write("\n".join(lines))

    print(f"\n  JSON → {json_path}")
    print(f"  Markdown → {md_path}")
    print(f"\nDone — {len(signals)} macro indicators monitored.")


if __name__ == "__main__":
    main()
