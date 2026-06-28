#!/usr/bin/env python3
"""Build truth_check.json from the saved baseline + cached SPY bars.

Reads:
  backtests/baseline_2026-06-28/baseline_B_slip10_atr.json  (strat curve + trades)
  backtest_harness/cache/SPY.json                          (Alpaca IEX bars)

Produces:
  truth_check.json with the exact 8 keys you specified.

IS/OOS split rule: chronological 70/30 of trading days, applied to the closed
trade ledger by exit_date. IS window = first 70% of trading days, OOS = last 30%.
IS/OOS total return = compound return of strat daily returns restricted to that
window. This is a single-run chronological split (no walk-forward retraining);
matches your spec "I am not changing harness logic."
"""
from __future__ import annotations

import json
import os
import sys
from typing import List

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(
    REPO, "backtests", "baseline_d_regime", "baseline_D_slip10_atr_regime.json"
)
SPY_CACHE = os.path.join(REPO, "backtest_harness", "cache", "SPY.json")
OUT = os.path.join(REPO, "truth_check.json")

SLIPPAGE_BPS = 10
IS_FRACTION = 0.70


def load_baseline(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def load_spy_bars(path: str, start: str, end: str) -> List[dict]:
    with open(path) as f:
        payload = json.load(f)
    return [b for b in payload["bars"] if start <= b["date"] <= end]


def equity_to_daily_returns(curve: List[dict]) -> List[float]:
    """oldest->newest equity curve -> list of decimal daily returns."""
    eq = [p["equity"] for p in curve]
    return [(b / a - 1) for a, b in zip(eq[:-1], eq[1:]) if a > 0]


def spy_bars_to_curve(bars: List[dict], start_equity: float) -> List[dict]:
    """Match harness spy_buy_hold: buy at first bar's open, mark to close daily."""
    if not bars:
        return []
    entry = bars[0].get("open") or bars[0]["close"]
    shares = start_equity / float(entry)
    return [{"date": b["date"], "equity": round(shares * float(b["close"]), 2)} for b in bars]


def compound_return(daily_returns: List[float]) -> float:
    eq = 1.0
    for r in daily_returns:
        eq *= (1 + r)
    return eq - 1


def main() -> int:
    rpt = load_baseline(BASELINE)
    window = rpt["config"]["window"]
    start_date, end_date = window["start"], window["end"]
    start_equity = float(rpt["config"]["start_equity"])

    strat_curve = rpt["equity_curve"]
    strat_dates = [p["date"] for p in strat_curve]
    n_days = len(strat_dates)

    spy_bars = load_spy_bars(SPY_CACHE, start_date, end_date)
    spy_curve = spy_bars_to_curve(spy_bars, start_equity)
    spy_dates = [p["date"] for p in spy_curve]
    assert strat_dates == spy_dates, (
        f"date mismatch: strat={len(strat_dates)} spy={len(spy_dates)}"
    )

    strat_daily = equity_to_daily_returns(strat_curve)
    spy_daily = equity_to_daily_returns(spy_curve)
    assert len(strat_daily) == len(spy_daily), (
        f"daily-return length mismatch: strat={len(strat_daily)} spy={len(spy_daily)}"
    )

    closed_trades = rpt["trades"]
    n_closed = len(closed_trades)
    closed_returns = [t["return_pct"] / 100.0 for t in closed_trades]

    # Chronological IS/OOS split on trading days.
    # IS = trading days [0, is_cutoff); OOS = trading days [is_cutoff, n_days).
    # IS/OOS total return = compound return of strat daily returns in that window.
    is_cutoff = int(n_days * IS_FRACTION)
    is_daily = strat_daily[:is_cutoff]
    oos_daily = strat_daily[is_cutoff:]
    is_return = compound_return(is_daily)
    oos_return = compound_return(oos_daily)

    payload = {
        "window": f"{start_date} to {end_date}",
        "slippage_bps": SLIPPAGE_BPS,
        "n_closed_trades": n_closed,
        "in_sample_total_return": round(is_return, 6),
        "out_of_sample_total_return": round(oos_return, 6),
        "strat_daily_returns": [round(r, 8) for r in strat_daily],
        "spy_daily_returns": [round(r, 8) for r in spy_daily],
        "closed_trade_returns": [round(r, 8) for r in closed_returns],
    }

    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"window:           {payload['window']}")
    print(f"slippage_bps:     {payload['slippage_bps']}")
    print(f"n_closed_trades:  {payload['n_closed_trades']}")
    print(f"in_sample:        {payload['in_sample_total_return']:+.4%}  (first {is_cutoff}/{n_days} trading days)")
    print(f"out_of_sample:    {payload['out_of_sample_total_return']:+.4%}  (last {n_days - is_cutoff}/{n_days} trading days)")
    print(f"strat_daily:      {len(payload['strat_daily_returns'])} entries")
    print(f"spy_daily:        {len(payload['spy_daily_returns'])} entries  (1:1 dates with strat)")
    print(f"closed_returns:   {len(payload['closed_trade_returns'])} entries")
    print(f"file:             {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())