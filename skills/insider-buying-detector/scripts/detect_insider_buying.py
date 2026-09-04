#!/usr/bin/env python3
"""Insider buying detector — yfinance insider transactions, scores conviction signals."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

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

EXEC_WEIGHTS = {
    "CEO": 1.5, "CFO": 1.4, "COO": 1.3, "President": 1.3,
    "Director": 1.0, "VP": 0.9, "Officer": 0.8,
}


def fetch_insider(symbol: str, days: int) -> list[dict]:
    """Fetch open-market insider purchases from yfinance's Form 4 transaction feed.

    yfinance's "Transaction" column is unreliable/blank on this data source; the
    "Text" field (e.g. "Purchase at price 21.12 per share.") is what actually
    carries the transaction type, so filter on that instead.
    """
    try:
        df = yf.Ticker(symbol).insider_transactions
        if df is None or df.empty:
            return []
        cutoff = date.today() - timedelta(days=days)
        out = []
        for _, row in df.iterrows():
            text = str(row.get("Text") or "").strip()
            if not text.lower().startswith("purchase"):
                continue
            start = row.get("Start Date")
            if pd.isna(start):
                continue
            txn_date = start.date()
            if txn_date < cutoff:
                continue
            shares = row.get("Shares") or 0
            value = row.get("Value")
            value = 0.0 if pd.isna(value) else float(value)
            price = (value / shares) if shares else 0.0
            out.append({
                "reportingName": row.get("Insider", "") or "",
                "typeOfOwner": row.get("Position", "") or "",
                "securitiesTransacted": float(shares) if shares else 0.0,
                "price": price,
                "filingDate": txn_date.isoformat(),
            })
        return out
    except Exception as exc:
        print(f"  yfinance error ({symbol}): {exc}", file=sys.stderr)
        return []


def score_transactions(symbol: str, txns: list[dict]) -> dict | None:
    if not txns:
        return None

    total_value = sum(
        (t.get("securitiesTransacted") or 0) * (t.get("price") or 0)
        for t in txns
    )
    unique_insiders = len({t.get("reportingName", "") for t in txns})
    transaction_count = len(txns)

    exec_score = 0.0
    for t in txns:
        role = t.get("typeOfOwner", "") or ""
        weight = 1.0
        for title, w in EXEC_WEIGHTS.items():
            if title.lower() in role.lower():
                weight = w
                break
        shares = t.get("securitiesTransacted") or 0
        price = t.get("price") or 0
        exec_score += shares * price * weight

    value_score = min(40, exec_score / 50_000)
    count_score = min(30, unique_insiders * 10)
    cluster_score = min(30, transaction_count * 5)
    conviction_score = round(value_score + count_score + cluster_score)

    grade = "A" if conviction_score >= 70 else (
        "B" if conviction_score >= 50 else (
            "C" if conviction_score >= 30 else "D"
        )
    )

    top_buyer = max(txns, key=lambda t: (t.get("securitiesTransacted") or 0) * (t.get("price") or 0))

    return {
        "symbol": symbol,
        "transactions": transaction_count,
        "unique_insiders": unique_insiders,
        "total_value_usd": round(total_value),
        "largest_buyer": top_buyer.get("reportingName", "?"),
        "largest_buyer_role": top_buyer.get("typeOfOwner", "?"),
        "largest_purchase_value": round(
            (top_buyer.get("securitiesTransacted") or 0) * (top_buyer.get("price") or 0)
        ),
        "conviction_score": conviction_score,
        "grade": grade,
        "most_recent_date": max(t.get("filingDate", "") for t in txns),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Insider Buying Detector (yfinance)")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--min-grade", choices=["A", "B", "C", "D"], default="C")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--output-dir", default="reports/")
    args = parser.parse_args()

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    print(f"Insider Buying Detector — {len(args.symbols)} symbols, {args.days} days")
    print("-" * 50)

    min_grade_rank = {"A": 0, "B": 1, "C": 2, "D": 3}[args.min_grade]

    results = []
    for sym in args.symbols:
        print(f"  {sym}...", end=" ", flush=True)
        txns = fetch_insider(sym, args.days)
        r = score_transactions(sym, txns)
        if r:
            grade_rank = {"A": 0, "B": 1, "C": 2, "D": 3}.get(r["grade"], 4)
            if grade_rank <= min_grade_rank:
                results.append(r)
                print(f"Grade={r['grade']} score={r['conviction_score']} insiders={r['unique_insiders']} value=${r['total_value_usd']:,}")
            else:
                print(f"grade {r['grade']} below threshold")
        else:
            print("no recent purchases")

    results.sort(key=lambda x: x["conviction_score"], reverse=True)
    top = results[: args.top]

    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": ts,
        "lookback_days": args.days,
        "min_grade": args.min_grade,
        "signals_found": len(results),
    }

    json_path = str(Path(args.output_dir) / f"insider_buying_{ts}.json")
    md_path = str(Path(args.output_dir) / f"insider_buying_{ts}.md")

    with open(json_path, "w") as f:
        json.dump({"metadata": metadata, "results": top}, f, indent=2, default=str)

    lines = [
        "# Insider Buying Detector",
        f"**Generated:** {metadata['generated_at']} | Lookback: {args.days} days | Min grade: {args.min_grade}",
        f"**Signals found:** {len(results)}",
        "",
        "| Symbol | Grade | Score | Insiders | Total Value | Top Buyer | Latest Filing |",
        "|--------|-------|-------|----------|-------------|-----------|---------------|",
    ]
    for r in top:
        lines.append(
            f"| {r['symbol']} | {r['grade']} | {r['conviction_score']} "
            f"| {r['unique_insiders']} | ${r['total_value_usd']:,} "
            f"| {r['largest_buyer']} | {r['most_recent_date']} |"
        )
    with open(md_path, "w") as f:
        f.write("\n".join(lines))

    print(f"\n  JSON → {json_path}")
    print(f"  Markdown → {md_path}")
    print(f"\nDone — {len(top)} insider buying signals.")


if __name__ == "__main__":
    main()
