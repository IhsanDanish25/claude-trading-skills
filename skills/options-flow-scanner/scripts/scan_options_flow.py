#!/usr/bin/env python3
"""Options flow scanner — flags unusual options activity by volume/OI ratio via yfinance."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("Error: yfinance not installed. Run: pip install yfinance", file=sys.stderr)
    sys.exit(1)

DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN",
    "TSLA", "NFLX", "CRM", "ADBE", "PANW", "CRWD",
]


def fetch_options(symbol: str, max_expiries: int = 3) -> list[dict]:
    """Fetch calls and puts for the nearest max_expiries expiry dates."""
    try:
        ticker = yf.Ticker(symbol)
        expiries = ticker.options
        if not expiries:
            return []
        cutoff = (date.today() + timedelta(days=90)).isoformat()
        near = [e for e in expiries if e <= cutoff][:max_expiries]
        contracts = []
        for exp in near:
            chain = ticker.option_chain(exp)
            dte = (date.fromisoformat(exp) - date.today()).days
            for row in chain.calls.itertuples():
                contracts.append(_row_to_dict(symbol, row, "CALL", exp, dte))
            for row in chain.puts.itertuples():
                contracts.append(_row_to_dict(symbol, row, "PUT", exp, dte))
        return contracts
    except Exception as exc:
        print(f"  yfinance error ({symbol}): {exc}", file=sys.stderr)
        return []


def _row_to_dict(symbol: str, row: object, opt_type: str, expiry: str, dte: int) -> dict:
    return {
        "symbol": symbol,
        "option_type": opt_type,
        "strike": getattr(row, "strike", None),
        "expiry": expiry,
        "dte": dte,
        "volume": int(getattr(row, "volume", 0) or 0),
        "open_interest": int(getattr(row, "openInterest", 0) or 0),
        "implied_volatility": float(getattr(row, "impliedVolatility", 0) or 0),
        "last_price": float(getattr(row, "lastPrice", 0) or 0),
        "in_the_money": bool(getattr(row, "inTheMoney", False)),
    }


def score_contract(c: dict, min_volume: int, min_oi_ratio: float) -> dict | None:
    volume = c["volume"]
    oi = max(c["open_interest"], 1)
    iv = c["implied_volatility"]
    dte = c["dte"]
    opt_type = c["option_type"]

    if volume < min_volume:
        return None

    oi_ratio = volume / oi
    if oi_ratio < min_oi_ratio:
        return None

    score = 0
    if oi_ratio >= 5.0:
        score += 40
    elif oi_ratio >= 3.0:
        score += 25
    elif oi_ratio >= 1.5:
        score += 10

    if volume >= 10_000:
        score += 30
    elif volume >= 2_000:
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
        "symbol": c["symbol"],
        "option_type": opt_type,
        "strike": c["strike"],
        "expiry": c["expiry"],
        "dte": dte,
        "volume": volume,
        "open_interest": c["open_interest"],
        "vol_oi_ratio": round(oi_ratio, 2),
        "iv": round(iv, 4),
        "last_price": c["last_price"],
        "in_the_money": c["in_the_money"],
        "score": score,
        "signal": "CALL_SWEEP" if opt_type == "CALL" else "PUT_SWEEP",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Options Flow Scanner (yfinance)")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--min-volume", type=int, default=100)
    parser.add_argument("--min-oi-ratio", type=float, default=1.5)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    print(f"Options Flow Scanner (yfinance) — {len(args.symbols)} symbols")
    print("-" * 50)

    results = []
    for sym in args.symbols:
        print(f"  {sym}...", end=" ", flush=True)
        contracts = fetch_options(sym)
        flagged = 0
        for c in contracts:
            scored = score_contract(c, args.min_volume, args.min_oi_ratio)
            if scored:
                results.append(scored)
                flagged += 1
        print(f"{flagged} unusual contracts")

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[: args.top]

    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": ts,
        "symbols_scanned": len(args.symbols),
        "min_volume": args.min_volume,
        "min_oi_ratio": args.min_oi_ratio,
        "total_unusual": len(results),
    }

    json_path = str(Path(args.output_dir) / f"options_flow_{ts}.json")
    md_path = str(Path(args.output_dir) / f"options_flow_{ts}.md")

    with open(json_path, "w") as f:
        json.dump({"metadata": metadata, "results": top}, f, indent=2, default=str)
    print(f"  JSON → {json_path}")

    lines = [
        "# Options Flow Scanner",
        f"**Generated:** {metadata['generated_at']} | Source: yfinance (free)",
        f"**Unusual contracts:** {len(results)} across {len(args.symbols)} symbols",
        "",
        "| Symbol | Type | Strike | Expiry | DTE | Volume | OI | Vol/OI | IV | Score |",
        "|--------|------|-------:|--------|----:|-------:|---:|-------:|---:|------:|",
    ]
    for r in top:
        lines.append(
            f"| {r['symbol']} | {r['option_type']} | ${r['strike']} | {r['expiry']} "
            f"| {r['dte']} | {r['volume']:,} | {r['open_interest']:,} "
            f"| {r['vol_oi_ratio']}x | {r['iv']:.1%} | {r['score']} |"
        )
    lines.extend(["", "*High Vol/OI ratio signals unusual positioning vs. existing open interest.*"])
    with open(md_path, "w") as f:
        f.write("\n".join(lines))
    print(f"  Markdown → {md_path}")

    print(f"\nDone — {len(top)} top unusual contracts.")


if __name__ == "__main__":
    main()
