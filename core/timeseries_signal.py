"""
Time-series directional forecast — a CONFIRMING FILTER, not a strategy.

Forecasts next-day direction from a symbol's recent daily closes. This is a
fundamentally different signal type from the bot's existing rule-based
strategies (RSI thresholds, breakout levels, earnings drift) — a
learned/fitted pattern rather than a fixed rule — so it's wired as an
independent confirming input, never its own entry path (see
TIMESERIES_ENABLED in core/config.py and the gate wiring in
routines/market_open.py).

Two model backends, selected via TIMESERIES_MODEL / the `model` kwarg:

- "arima": ARIMA(1,1,1) on price levels. Standalone-backtested
  (backtests/timeseries_standalone_arima_2026-08-10/summary.md) and found to
  never clear the live 60%-confidence gate — mean confidence 9.7%, max
  observed 43.4% across a 156-sample diagnostic. Kept for reference/
  comparison, not the active default.
- "lstm": a tiny 1-layer LSTM classifier on a short window of daily
  *returns* (not price levels), predicting next-day up/down. Trained fresh
  on the provided history each call — genuinely different signal type
  (learned nonlinear pattern vs. a linear time-series model), per the
  task's fallback option. This is the current TIMESERIES_MODEL default,
  but is UNVALIDATED until its own standalone backtest run — see the same
  harness (scripts/backtest_timeseries_standalone.py) before trusting it
  any more than ARIMA was trusted.

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

from core.config import (
    TIMESERIES_LSTM_EPOCHS,
    TIMESERIES_LSTM_LOOKBACK,
    TIMESERIES_MIN_HISTORY_DAYS,
)
from core.config import TIMESERIES_MODEL as _DEFAULT_MODEL

log = logging.getLogger(__name__)

_NEUTRAL_RESULT = {
    "direction": "neutral",
    "confidence": 0.0,
    "predicted_pct_change": 0.0,
}


def _direction_from_pct_change(pct_change: float) -> str:
    if pct_change > 0:
        return "long"
    if pct_change < 0:
        return "short"
    return "neutral"


def _forecast_arima(closes: list[float]) -> dict:
    """ARIMA(1,1,1) 1-step-ahead forecast. `confidence` is derived from how
    many forecast standard errors the predicted move is from zero (a proxy
    for how seriously the model takes its own prediction, not a probability
    of correctness) — squashed into [0, 1) via z / (z + 1)."""
    try:
        from statsmodels.tsa.arima.model import ARIMA

        model = ARIMA(closes, order=(1, 1, 1))
        fit = model.fit()
        forecast_res = fit.get_forecast(steps=1)
        pred = float(forecast_res.predicted_mean[0])
        se = float(forecast_res.se_mean[0])
    except Exception as e:
        log.warning("_forecast_arima: fit/forecast failed: %s", e)
        return {**_NEUTRAL_RESULT, "reason": f"model_error: {e}"}

    last = closes[-1]
    if not last:
        return {**_NEUTRAL_RESULT, "reason": "zero_last_close"}

    pct_change = (pred - last) / last
    z = abs(pred - last) / se if se > 0 else 0.0
    confidence = z / (z + 1.0)

    return {
        "direction": _direction_from_pct_change(pct_change),
        "confidence": round(confidence, 4),
        "predicted_pct_change": round(pct_change, 6),
    }


def _forecast_lstm(
    closes: list[float],
    lookback: int = TIMESERIES_LSTM_LOOKBACK,
    epochs: int = TIMESERIES_LSTM_EPOCHS,
) -> dict:
    """Tiny 1-layer LSTM classifier trained fresh on `closes`' daily
    returns, predicting next-day up/down. `confidence` is |p - 0.5| * 2,
    where p is the model's sigmoid output for "up" (0.5 = pure coin-flip,
    1.0 = maximally confident either way) — a genuine, if crude,
    probability-flavored confidence, unlike ARIMA's standard-error proxy.

    Needs at least `lookback` + 10 return-observations to form a handful of
    training sequences; below that, returns neutral rather than training on
    almost nothing.
    """
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]
    ]
    if len(returns) < lookback + 10:
        return {**_NEUTRAL_RESULT, "reason": "insufficient_history_for_lstm"}

    try:
        import torch
        from torch import nn

        torch.manual_seed(0)

        X, y = [], []
        for i in range(lookback, len(returns)):
            X.append(returns[i - lookback : i])
            y.append(1.0 if returns[i] > 0 else 0.0)
        X_t = torch.tensor(X, dtype=torch.float32).unsqueeze(-1)  # (N, lookback, 1)
        y_t = torch.tensor(y, dtype=torch.float32).unsqueeze(-1)  # (N, 1)

        class _TinyLSTM(nn.Module):
            def __init__(self):
                super().__init__()
                self.lstm = nn.LSTM(input_size=1, hidden_size=16, num_layers=1, batch_first=True)
                self.head = nn.Linear(16, 1)

            def forward(self, x):
                _, (h_n, _) = self.lstm(x)
                return torch.sigmoid(self.head(h_n[-1]))

        net = _TinyLSTM()
        optimizer = torch.optim.Adam(net.parameters(), lr=0.01)
        loss_fn = nn.BCELoss()

        net.train()
        for _ in range(epochs):
            optimizer.zero_grad()
            pred = net(X_t)
            loss = loss_fn(pred, y_t)
            loss.backward()
            optimizer.step()

        net.eval()
        with torch.no_grad():
            last_window = torch.tensor(returns[-lookback:], dtype=torch.float32).view(
                1, lookback, 1
            )
            p_up = float(net(last_window).item())
    except Exception as e:
        log.warning("_forecast_lstm: train/predict failed: %s", e)
        return {**_NEUTRAL_RESULT, "reason": f"model_error: {e}"}

    confidence = abs(p_up - 0.5) * 2.0
    pct_change = returns[-1] if returns else 0.0  # last observed return, informational only
    direction = "long" if p_up > 0.5 else ("short" if p_up < 0.5 else "neutral")

    return {
        "direction": direction,
        "confidence": round(confidence, 4),
        "predicted_pct_change": round(pct_change, 6),
        "p_up": round(p_up, 4),
    }


_MODELS = {"arima": _forecast_arima, "lstm": _forecast_lstm}


def forecast_direction(bars_newest_first: list[dict], model: str | None = None) -> dict:
    """1-step-ahead directional forecast from daily close bars.

    `bars_newest_first`: list of {"close": float, ...} dicts, index 0 = most
    recent bar — the shape core.screener.fetch_bars() and
    BarStore.slice_asof() both already return.

    `model`: "arima" or "lstm" (defaults to TIMESERIES_MODEL from config).

    Returns {"direction": "long"|"short"|"neutral", "confidence": float in
    [0, 1], "predicted_pct_change": float, ...}.
    """
    model = model or _DEFAULT_MODEL
    fn = _MODELS.get(model)
    if fn is None:
        return {**_NEUTRAL_RESULT, "reason": f"unknown_model: {model}"}

    closes = [b["close"] for b in bars_newest_first if b.get("close") is not None]
    closes.reverse()  # oldest -> newest

    if len(closes) < TIMESERIES_MIN_HISTORY_DAYS:
        return {**_NEUTRAL_RESULT, "reason": "insufficient_history"}

    return fn(closes)


def confirms(
    proposed_direction: str,
    bars_newest_first: list[dict],
    min_confidence: float,
    model: str | None = None,
) -> dict:
    """Does the time-series model confirm a strategy's proposed direction?

    "Confirms" means: agrees, OR is neutral, OR disagrees but isn't
    confident enough (< min_confidence) for that disagreement to count.
    Only a confident disagreement blocks. Returns {"allowed": bool, **forecast}.
    """
    result = forecast_direction(bars_newest_first, model=model)
    disagrees = (
        result["direction"] != "neutral"
        and result["direction"] != proposed_direction
        and result["confidence"] >= min_confidence
    )
    return {"allowed": not disagrees, **result}
