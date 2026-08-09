# Time-series Confirming Filter — LSTM Backend Standalone Validation (Reduced Scope)

**Status: DOES NOT CLEAR THE VALIDATION BAR. `TIMESERIES_ENABLED` stays `False`.**

Follow-up to the ARIMA validation (`backtests/timeseries_standalone_arima_2026-08-10/summary.md`,
which also failed). This tests the task's fallback option: a tiny 1-layer
LSTM classifier on daily return sequences, `core.timeseries_signal._forecast_lstm`.

## What was tested

A 1-layer LSTM (hidden size 16) trained fresh on each symbol's recent daily
*returns* (not price levels, unlike ARIMA) to classify next-day up/down,
using the same 20-day lookback / 30-epoch defaults live trading would use
(`TIMESERIES_LSTM_LOOKBACK`, `TIMESERIES_LSTM_EPOCHS`). Backtested standalone
through the identical harness/gates as ARIMA and breakout/meanrev/earnmom,
via `scripts/backtest_timeseries_standalone.py` (now dispatches on
`TIMESERIES_MODEL`, currently `"lstm"`).

**Same scope caveat as the ARIMA run:** 12 liquid large-caps, 2-year window
(2024-07-02 → 2026-07-02), not the full ~440-symbol/2020-2026 window —
runtime tractability (each fit/train ~1.1s, comparable to ARIMA).

## Result

At the live default confidence threshold (`TIMESERIES_MIN_CONFIDENCE=0.6`):

- **0 signals generated** — same outcome as ARIMA, never once reached 60%
  confidence across the full 2-year window on 12 symbols.

Confidence-distribution diagnostic (156 samples, same universe/window/sample
dates as the ARIMA diagnostic, for direct comparison):

| Metric | ARIMA | LSTM |
|---|---|---|
| Mean confidence | 9.7% | 11.8% |
| Median confidence | 7.2% | 10.4% |
| Max confidence observed | 43.4% | 37.2% |
| Fraction ≥ 60% (live threshold) | 0.0% | 0.0% |
| Fraction ≥ 30% | 5.1% | 3.2% |
| Direction split | 98 long / 58 short / 0 neutral | 105 long / 51 short / 0 neutral |

## Interpretation

The LSTM is not meaningfully better than ARIMA here — both sit in the same
"essentially no edge" regime, which is the expected result given what the
model actually sees: a single-feature (return magnitude), 20-day window,
trained from scratch on ~200-500 in-window examples per symbol with no
volume, volatility, or cross-sectional features. That's not enough signal
for any model architecture to reliably beat near-random-walk daily returns —
switching from a linear (ARIMA) to a nonlinear (LSTM) model doesn't help when
the bottleneck is the input features and training data, not model capacity.

This is the same conclusion as the ARIMA run, arrived at independently: the
task asked to validate this "exactly as strictly as PEAD was" — like PEAD,
this fails the bar and should not be enabled.

## What would be needed before either backend could pass

Both attempts (ARIMA and LSTM) point at the same gap: neither has real
predictive features, just raw price/return history. A future attempt would
need genuinely new information content — volume, volatility regime,
cross-sectional/relative-strength features, or a longer training history
per symbol (currently limited by `TIMESERIES_MIN_HISTORY_DAYS`) — not
just a different model class on the same thin inputs.

## Current state (unchanged live behavior)

- `TIMESERIES_ENABLED=False` (default, untouched).
- `TIMESERIES_MODEL=lstm` is now the active default backend (swapped from
  `arima`), but since the filter is fully disabled, this has no live effect.
- The confirming-filter wiring in `routines/market_open.py` is unchanged and
  fully tested — breakout/meanrev/earnmom/insider behave exactly as before
  this branch, regardless of which backend `TIMESERIES_MODEL` names.
