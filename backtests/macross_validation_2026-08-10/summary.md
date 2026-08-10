# MA Crossover (Trend-Following) — Standalone Validation

**Status: DOES NOT CLEAR THE VALIDATION BAR. Not added to `STRATEGY_MODE`.**

## What was tested

`core/macross_screener.py` — a 20/50-day SMA golden cross, entered when the
fast average crosses above the slow average within the last 3 trading days,
confirmed by cross-day volume >= 1.2x its own trailing average, with price
still above the slow SMA. Genuinely different signal type from the other
three active strategies (breakout reacts to a resistance level, mean
reversion to RSI/Bollinger extremes, earnings momentum to a fundamental
catalyst) — this one reacts purely to trend structure.

Backtested standalone through the same harness/gates as breakout/meanrev/
earnmom (`backtest_5_strategies.py`, `backtest_harness/satellite_signals.py:
get_historical_macross_signals`), full committed universe (509 symbols, no
network), full window (2020-10-13 → 2026-07-02, ~6 years).

## Result

| Metric | Value |
|---|---|
| Signals generated | 5,852 |
| Trades taken | 468 |
| Win rate | 49.4% |
| Sharpe | 0.63 |
| Max drawdown | -7.5% |
| Avg win / avg loss | +6.28% / -4.77% |
| **p-value** | **0.1324** (need < 0.05) |
| Overfit ratio (IS/OOS) | 0.48 (need ≤ 1.5 — passes) |

### Gates

| Gate | Result |
|---|---|
| Trade count (≥50) | **PASS** (468) |
| Not overfit (IS/OOS ≤1.5) | **PASS** (0.48) |
| Significant (p<0.05) | **FAIL** (p=0.1324) |
| Beats SPY | **FAIL** (+19.0% vs SPY +130.1%; Sharpe 0.63 vs 0.95) |

**Trustworthy: False.** Fails the significance gate — the trade-count and
overfit checks pass cleanly, but a p-value of 0.13 means there's roughly a
1-in-8 chance this return pattern is noise, well above the 1-in-20 bar every
other active strategy had to clear.

## Interpretation

468 trades is a healthy sample and the strategy isn't overfit (IS/OOS ratio
well under the 1.5 ceiling), so this isn't a "too little data" or "curve-fit
to one regime" problem — the edge itself is just too weak to distinguish
from chance at this sample size. A 49.4% win rate with a 1.32:1 win/loss
ratio produces a real but thin per-trade expectancy (+0.69%), and that's
consistent with the significance test's verdict: plausible edge, not
demonstrated edge.

This is the same honest-negative-result pattern as the time-series
confirming filter's own validation (ARIMA and LSTM, both failed on
confidence rather than significance) — the strategy was built correctly and
tested exactly as strictly as the four that passed, and it didn't clear the
bar. Per the "validate as strictly as PEAD was" standard, it stays out of
the default strategy set.

## What would be needed before this could pass

Not a parameter tweak on the same signal — the underlying edge (a fast/slow
SMA cross with basic volume confirmation) is a well-known, heavily-traded
pattern with limited alpha left in it on large-cap names at daily
resolution. A future attempt would need either a genuinely different
confirmation layer (e.g. sector/market-relative strength, volatility regime
filtering) or a different, less commoditized trend signal — not just
retuning the 20/50 lookback or the volume threshold on this same setup.

## Current state (unchanged live behavior)

- `macross` is registered as a valid `STRATEGY_MODE` value (opt-in) but is
  **not** in the default `breakout,meanrev,earnmom,insider` list.
- The screener, gate wiring (`_run_macross` in `routines/market_open.py`),
  and config (`core/config.py` `MACROSS_*` block) all exist and are tested,
  but nothing changes for the live bot unless `STRATEGY_MODE` is explicitly
  set to include `macross`.
