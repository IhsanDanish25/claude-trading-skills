# Time-series Confirming Filter — Standalone Validation (Reduced Scope)

**Status: DOES NOT CLEAR THE VALIDATION BAR. `TIMESERIES_ENABLED` stays `False`.**

## What was tested

`core/timeseries_signal.py` fits ARIMA(1,1,1) on a symbol's recent daily
closes and forecasts the next close, producing a direction (long/short/neutral)
and a confidence score (0-1). Per the task spec, it was backtested standalone
(as if it generated its own entries) through the same harness used for
breakout/meanrev/earnmom, via `scripts/backtest_timeseries_standalone.py`.

**Scope note:** this run used 12 liquid large-caps over the last 2 years
(2024-07-02 → 2026-07-02), not the full ~440-symbol/2020-2026 window used for
breakout/meanrev/earnmom. ARIMA refits cost ~1s each; the full universe/window
at the harness's normal cadence would run for hours. If this is ever revisited,
scale the universe/window before treating a "pass" as final — but see below,
the result here is unlikely to change with more data.

## Result

At the live default confidence threshold (`TIMESERIES_MIN_CONFIDENCE=0.6`):

- **0 signals generated** across the entire 2-year window on 12 symbols —
  the model never once reached 60% confidence.

A follow-up diagnostic (confidence distribution, not gated by the threshold)
sampled 156 (symbol, date) points across the same universe/window:

| Metric | Value |
|---|---|
| Mean confidence | 9.7% |
| Median confidence | 7.2% |
| Max confidence observed | 43.4% |
| Fraction ≥ 60% (live threshold) | 0.0% |
| Fraction ≥ 30% | 5.1% |
| Direction split | 98 long / 58 short / 0 neutral |

## Interpretation

This is a real, informative negative result, not a bug or a threshold
tuning problem. An ARIMA(1,1,1) fit on raw daily close *levels* (not
returns, not volume-augmented, no exogenous features) is a well-known weak
baseline — it's essentially predicting "tomorrow's close ≈ today's close
plus a tiny drift," which produces forecasts with wide confidence intervals
relative to the predicted move on real (near-random-walk) equity closes.
The single-fit sanity check on SPY showed 0.11% confidence; the 156-sample
diagnostic confirms that wasn't a fluke.

Lowering the confidence threshold to force trades through would defeat the
purpose of the gate (it exists specifically so only genuinely confident
disagreement can block a validated strategy) and wouldn't fix the underlying
issue: the model isn't confident because it doesn't have real predictive
power in this form, not because the bar is miscalibrated.

## What would be needed before this could pass

Not a threshold change — a materially different model or feature set:
returns instead of price levels, volume/volatility features, a longer or
different order search (auto-ARIMA), or the LSTM alternative the task
mentioned as a fallback option. Any of those would need their own standalone
backtest through this same harness before reconsideration.

## Current state (unchanged live behavior)

- `TIMESERIES_ENABLED=False` (default, untouched).
- The confirming-filter wiring in `routines/market_open.py` exists and is
  fully tested, but is a no-op while the flag is off — breakout/meanrev/
  earnmom/insider behave exactly as before this branch.
