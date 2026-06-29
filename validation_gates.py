"""
validation_gates.py  —  strong-charisma drop-in  (THE HONESTY MACHINE)

This is the weapon that beats every loud "I beat the market!" Medium guy:
they show in-sample dream numbers; you show numbers that SURVIVED these gates.

Bolt this onto your existing ATR(14)-stop + slippage(0/10/20bps) backtest harness.
It does NOT replace your harness — it JUDGES its output and stamps PASS/FAIL so a
flattering-but-fake result can't fool you.

Four gates:
  1. TRADE-COUNT GATE  : n >= 50. Below that, results are noise, not edge.
  2. OVERFIT GATE      : in-sample/out-of-sample return ratio <= 1.5.
                         If IS crushes OOS, you curve-fit the past.
                         Both IS and OOS must be positive for the ratio to
                         mean anything — a loss in both windows trivially
                         satisfies the ratio check without proving edge.
  3. SIGNIFICANCE GATE : t-stat on mean daily return; p < 0.05 (two-sided).
  4. BENCHMARK GATE    : must clear SPY on the SAME window AFTER costs,
                         on BOTH total return and risk-adjusted (Sharpe within
                         tolerance OR drawdown materially better).

A result is TRUSTWORTHY only if gates 1-3 pass. It's a WIN vs the field only if
gate 4 also passes. numpy optional; falls back to pure python.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, Dict

MIN_TRADES = 50
OVERFIT_MAX_RATIO = 1.5
P_VALUE_MAX = 0.05


@dataclass
class GateReport:
    n_trades: int
    overfit_ratio: float
    p_value: float
    strat_total_return: float
    spy_total_return: float
    strat_sharpe: float
    spy_sharpe: float
    strat_max_dd: float
    spy_max_dd: float
    gates: Dict[str, bool] = field(default_factory=dict)

    @property
    def trustworthy(self):
        return all(self.gates.get(k, False) for k in
                   ("trade_count", "not_overfit", "significant"))

    @property
    def beats_field(self):
        return self.trustworthy and self.gates.get("beats_spy", False)

    def summary(self):
        def mark(b): return "PASS" if b else "FAIL"
        lines = [
            "=== VALIDATION GATES ===",
            f"[{mark(self.gates.get('trade_count'))}] trade count  : {self.n_trades} (need >= {MIN_TRADES})",
            f"[{mark(self.gates.get('not_overfit'))}] overfit       : IS/OOS ratio {self.overfit_ratio:.2f} (need <= {OVERFIT_MAX_RATIO})",
            f"[{mark(self.gates.get('significant'))}] significance  : p={self.p_value:.4f} (need < {P_VALUE_MAX})",
            f"[{mark(self.gates.get('beats_spy'))}] vs SPY        : ret {self.strat_total_return:+.1%} vs {self.spy_total_return:+.1%} | "
            f"Sharpe {self.strat_sharpe:.2f} vs {self.spy_sharpe:.2f} | maxDD {self.strat_max_dd:.1%} vs {self.spy_max_dd:.1%}",
            "",
            f"TRUSTWORTHY (not fooling yourself): {self.trustworthy}",
            f"BEATS THE FIELD (clears SPY too)  : {self.beats_field}",
        ]
        return "\n".join(lines)


# ---- stats helpers (pure python) ------------------------------------------
def _mean(x): return sum(x) / len(x) if x else 0.0

def _std(x):
    if len(x) < 2:
        return 0.0
    m = _mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))

def sharpe(daily_returns, rf=0.0, ann=252):
    if not daily_returns:
        return 0.0
    sd = _std(daily_returns)
    if sd == 0:
        return 0.0
    return (_mean(daily_returns) - rf) / sd * math.sqrt(ann)

def total_return(daily_returns):
    eq = 1.0
    for r in daily_returns:
        eq *= (1 + r)
    return eq - 1

def max_drawdown(daily_returns):
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in daily_returns:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return mdd

def _t_p_value(daily_returns):
    """Two-sided p-value that mean daily return != 0 (Student-t, normal approx tail)."""
    n = len(daily_returns)
    if n < 3:
        return 1.0
    sd = _std(daily_returns)
    if sd == 0:
        return 1.0
    t = _mean(daily_returns) / (sd / math.sqrt(n))
    # normal approximation to the t tail (fine for n>=30; conservative below)
    z = abs(t)
    p = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))
    return max(0.0, min(1.0, p))


# ---- the gate runner -------------------------------------------------------
def run_gates(
    strat_daily_returns: List[float],   # AFTER slippage+costs, full window
    spy_daily_returns: List[float],     # SAME window
    n_trades: int,
    is_return: Optional[float] = None,  # in-sample total return (walk-forward train)
    oos_return: Optional[float] = None, # out-of-sample total return (walk-forward test)
    sharpe_tolerance: float = 0.0,      # allow tie within this on Sharpe
) -> GateReport:
    strat_ret = total_return(strat_daily_returns)
    spy_ret = total_return(spy_daily_returns)
    strat_sh = sharpe(strat_daily_returns)
    spy_sh = sharpe(spy_daily_returns)
    strat_dd = max_drawdown(strat_daily_returns)
    spy_dd = max_drawdown(spy_daily_returns)
    p = _t_p_value(strat_daily_returns)

    # Overfit ratio. If you don't pass IS/OOS, it's marked unknown -> FAIL (be strict).
    if is_return is not None and oos_return not in (None, 0):
        ratio = is_return / oos_return if oos_return != 0 else float("inf")
    else:
        ratio = float("inf")

    # Gate 4: beat SPY on total return, AND (Sharpe not worse beyond tolerance
    # OR drawdown materially better — the drawdown-control escape hatch).
    beats_return = strat_ret > spy_ret
    sharpe_ok = strat_sh >= spy_sh - sharpe_tolerance
    dd_better = strat_dd > spy_dd  # less negative = shallower drawdown = better
    beats_spy = beats_return and (sharpe_ok or dd_better)

    # The ratio test only means something when the strategy is actually
    # positive in both windows. Without this guard, a loss-loss scenario
    # (e.g. IS=-1.83% / OOS=-1.49%) auto-passes the ratio<=1.5 check
    # without telling us anything about whether the strategy has edge.
    both_positive = (is_return is not None and oos_return is not None
                     and is_return > 0 and oos_return > 0)

    gates = {
        "trade_count": n_trades >= MIN_TRADES,
        "not_overfit": both_positive and ratio <= OVERFIT_MAX_RATIO,
        "significant": p < P_VALUE_MAX,
        "beats_spy": beats_spy,
    }
    return GateReport(
        n_trades=n_trades, overfit_ratio=ratio, p_value=p,
        strat_total_return=strat_ret, spy_total_return=spy_ret,
        strat_sharpe=strat_sh, spy_sharpe=spy_sh,
        strat_max_dd=strat_dd, spy_max_dd=spy_dd, gates=gates,
    )
