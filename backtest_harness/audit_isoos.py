#!/usr/bin/env python3
"""IS/OOS audit — read-only measurement across all baseline artifacts.

For each baseline_<scenario>.json, compute the same 70/30 chronological split
that run_backtest.py uses for E4, then report per-scenario:
  - IS return (start → 70% mark)
  - OOS return (70% mark → end)
  - Ratio (IS / OOS)  ← what validation_gates currently uses
  - IS annualized CAGR
  - OOS annualized CAGR
  - Annualized ratio (CAGR_IS / CAGR_OOS)  ← window-length-normalized

Does NOT modify any artifact or the harness. Writes one JSON summary file
into backtests/baseline_<today>/isoos_audit.json.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import sys
from typing import Optional

REPO = "/Users/mohamedihsan/claude-trading-skills"

OVERFIT_MAX_RATIO = 1.5  # from validation_gates.py
MIN_WINDOW_FOR_GATE = 100  # daily returns — below this, IS/OOS is noise


def annualized_return(total_return: float, days: int) -> float:
    """Compound annual return from a total-return decimal over N trading days."""
    if days <= 0:
        return 0.0
    return (1.0 + total_return) ** (252.0 / days) - 1.0


def compute_isoos(equity_curve: list, start_equity: float) -> dict:
    """Mirror the run_backtest.py E4 calculation, but also compute annualized CAGRs."""
    n = len(equity_curve)
    if n < 3:
        return {"status": "too_short", "n": n}

    split_i = max(1, int(n * 0.70))

    is_eq = equity_curve[split_i]["equity"]
    oos_eq = equity_curve[-1]["equity"]
    is_days = split_i  # trading days from start to split
    oos_days = n - split_i  # trading days from split to end

    is_return = is_eq / start_equity - 1
    oos_return = (oos_eq / is_eq - 1) if is_eq > 0 else None

    is_cagr = annualized_return(is_return, is_days) if is_days > 0 else 0.0
    oos_cagr = annualized_return(oos_return, oos_days) if (oos_return is not None and oos_days > 0) else 0.0

    ratio_cum = is_return / oos_return if (oos_return is not None and oos_return != 0) else None
    ratio_cagr = is_cagr / oos_cagr if (oos_cagr != 0) else None

    return {
        "status": "ok",
        "n_total": n,
        "split_index": split_i,
        "is_days": is_days,
        "oos_days": oos_days,
        "start_date": equity_curve[0]["date"],
        "split_date": equity_curve[split_i]["date"],
        "end_date": equity_curve[-1]["date"],
        "start_equity": start_equity,
        "is_end_equity": is_eq,
        "oos_end_equity": oos_eq,
        "is_total_return": is_return,
        "oos_total_return": oos_return,
        "is_cagr": is_cagr,
        "oos_cagr": oos_cagr,
        "ratio_cumulative": ratio_cum,
        "ratio_annualized": ratio_cagr,
        "gate_passes_ratio_cum": (ratio_cum is not None and ratio_cum <= OVERFIT_MAX_RATIO),
        "gate_passes_ratio_cagr": (ratio_cagr is not None and ratio_cagr <= OVERFIT_MAX_RATIO),
        "window_meaningful": n >= MIN_WINDOW_FOR_GATE,
    }


def main() -> int:
    artifacts = sorted(glob.glob(os.path.join(REPO, "backtests", "baseline_*", "baseline_*.json")))
    # Also pick up the older baseline_d_regime directory if present
    artifacts += sorted(glob.glob(os.path.join(REPO, "backtests", "baseline_d_regime", "baseline_*.json")))
    artifacts = sorted(set(artifacts))

    if not artifacts:
        print("No baseline artifacts found under backtests/")
        return 1

    print(f"Auditing {len(artifacts)} artifacts\n")

    results = []
    for path in artifacts:
        rel = os.path.relpath(path, REPO)
        try:
            with open(path) as fp:
                d = json.load(fp)
        except Exception as e:
            print(f"  SKIP {rel}: {e}")
            continue

        scenario = d.get("scenario", os.path.basename(path).replace("baseline_", "").replace(".json", ""))
        strat = d.get("strategy", {})
        ec = d.get("equity_curve", [])
        # For earnings scenarios start_equity is in config; for VCP it's also in config
        start_equity = d.get("config", {}).get("start_equity", 100_000.0)

        # The first equity_curve value may differ from start_equity (e.g. E4 starts at 99900.10 vs config 100000)
        # Use the actual first curve point as the IS denominator so the calc matches the equity curve.
        if ec:
            actual_start = ec[0]["equity"]
        else:
            actual_start = start_equity

        audit = compute_isoos(ec, actual_start)

        row = {
            "artifact": rel,
            "scenario": scenario,
            "directory": os.path.basename(os.path.dirname(path)),
            "strategy_total_return": strat.get("total_return_pct"),
            "strategy_sharpe": strat.get("sharpe"),
            "trading_days": strat.get("trading_days"),
            "isoos": audit,
        }
        results.append(row)

        # one-line print
        if audit["status"] == "ok":
            rc = audit["ratio_cumulative"]
            ra = audit["ratio_annualized"]
            gate_cum = "PASS" if audit["gate_passes_ratio_cum"] else "FAIL"
            gate_cagr = "PASS" if audit["gate_passes_ratio_cagr"] else "FAIL"
            warn = "" if audit["window_meaningful"] else "  [WINDOW TOO SHORT]"
            print(
                f"  {scenario:<40} IS {audit['is_total_return']*100:+7.2f}%  "
                f"OOS {audit['oos_total_return']*100:+7.2f}%  "
                f"ratio={rc:5.2f} ({gate_cum})  ann_ratio={ra:5.2f} ({gate_cagr}){warn}"
            )
        else:
            print(f"  {scenario:<40} {audit['status']}")

    # write audit
    today = datetime.date.today().isoformat()
    out_dir = os.path.join(REPO, "backtests", f"baseline_{today}")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "isoos_audit.json")
    with open(out_path, "w") as fp:
        json.dump({
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "purpose": "Read-only IS/OOS audit. Mirrors run_backtest.py E4 split (70/30 chronological) "
                       "and adds annualized CAGR-based ratio (window-length-normalized).",
            "OVERFIT_MAX_RATIO": OVERFIT_MAX_RATIO,
            "MIN_WINDOW_FOR_GATE": MIN_WINDOW_FOR_GATE,
            "n_artifacts_audited": len(results),
            "results": results,
        }, fp, indent=2)
    print(f"\nWrote {os.path.relpath(out_path, REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
