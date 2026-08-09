"""
Time-series directional forecast — a CONFIRMING FILTER, not a strategy.

Fits a lightweight ARIMA(1,1,1) model on a symbol's recent daily closes and
forecasts the next close. This is a fundamentally different signal type from
the bot's existing rule-based strategies (RSI thresholds, breakout levels,
earnings drift) — a learned/fitted pattern rather than a fixed rule — so it's
wired as an independent confirming input, never its own entry path (see
TIMESERIES_ENABLED in core/config.py and the gate wiring in
routines/market_open.py).

forecast_direction() takes bars in the same "newest-first" shape both the
live fetcher (core.screener.fetch_bars) and the backtest point-in-time
slicer (backtest_harness/data.py:BarStore.slice_asof) already return, so the
exact same function backs both the live gate and the standalone backtest
signal generator (backtest_harness/satellite_signals.py) — no duplicated
model logic between the two.

Never raises: any fit/data failure returns direction="neutral",
confidence=0.0, which is a safe no-op everywhere this is consumed (a
confirming filter that abstains never blocks an already-validated
strategy's trade).
"""

from __future__ import annotations

import logging

from core.config import TIMESERIES_MIN_HISTORY_DAYS

log = logging.getLogger(__name__)

_NEUTRAL_RESULT = {
    "direction": "neutral",
    "confidence": 0.0,
    "predicted_pct_change": 0.0,
}


def forecast_direction(bars_newest_first: list[dict]) -> dict:
    """ARIMA(1,1,1) 1-step-ahead forecast from daily close bars.

    `bars_newest_first`: list of {"close": float, ...} dicts, index 0 = most
    recent bar — the shape core.screener.fetch_bars() and
    BarStore.slice_asof() both already return.

    Returns {"direction": "long"|"short"|"neutral", "confidence": float in
    [0, 1], "predicted_pct_change": float}. "confidence" is derived from how
    many forecast standard errors the predicted move is from zero (a proxy
    for how seriously the model takes its own prediction, not a probability
    of correctness) — squashed into [0, 1) via z / (z + 1).
    """
    closes = [b["close"] for b in bars_newest_first if b.get("close") is not None]
    closes.reverse()  # oldest -> newest, what ARIMA needs

    if len(closes) < TIMESERIES_MIN_HISTORY_DAYS:
        return {**_NEUTRAL_RESULT, "reason": "insufficient_history"}

    try:
        from statsmodels.tsa.arima.model import ARIMA

        model = ARIMA(closes, order=(1, 1, 1))
        fit = model.fit()
        forecast_res = fit.get_forecast(steps=1)
        pred = float(forecast_res.predicted_mean[0])
        se = float(forecast_res.se_mean[0])
    except Exception as e:
        log.warning("forecast_direction: ARIMA fit/forecast failed: %s", e)
        return {**_NEUTRAL_RESULT, "reason": f"model_error: {e}"}

    last = closes[-1]
    if not last:
        return {**_NEUTRAL_RESULT, "reason": "zero_last_close"}

    pct_change = (pred - last) / last
    z = abs(pred - last) / se if se > 0 else 0.0
    confidence = z / (z + 1.0)

    if pct_change > 0:
        direction = "long"
    elif pct_change < 0:
        direction = "short"
    else:
        direction = "neutral"

    return {
        "direction": direction,
        "confidence": round(confidence, 4),
        "predicted_pct_change": round(pct_change, 6),
    }


def confirms(
    proposed_direction: str,
    bars_newest_first: list[dict],
    min_confidence: float,
) -> dict:
    """Does the time-series model confirm a strategy's proposed direction?

    "Confirms" means: agrees, OR is neutral, OR disagrees but isn't
    confident enough (< min_confidence) for that disagreement to count.
    Only a confident disagreement blocks. Returns {"allowed": bool, **forecast}.
    """
    result = forecast_direction(bars_newest_first)
    disagrees = (
        result["direction"] != "neutral"
        and result["direction"] != proposed_direction
        and result["confidence"] >= min_confidence
    )
    return {"allowed": not disagrees, **result}
