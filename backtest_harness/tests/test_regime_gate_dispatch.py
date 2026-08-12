"""Unit tests for backtest_harness.regime_gate_dispatch - lets the backtest
engines select between the live SMA/ADX gate and the opt-in HMM gate without
needing real market data or network access.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backtest_harness.regime_gate_dispatch import classify_spy_regime


def _bars(n=300, seed=7, regime_len=60, with_volume=True):
    """Alternating calm/stressed regimes give the HMM genuine clusters to
    fit (mirrors tests/test_regime_gate_hmm.py's _bars()) - a plain
    unstructured random walk is prone to near-singular covariance fits."""
    rng = np.random.default_rng(seed)
    log_returns = np.empty(n)
    pos, calm = 0, True
    while pos < n:
        length = min(regime_len, n - pos)
        if calm:
            log_returns[pos : pos + length] = rng.normal(0.0006, 0.006, length)
        else:
            log_returns[pos : pos + length] = rng.normal(-0.0012, 0.028, length)
        calm = not calm
        pos += length

    closes = list(100 * np.exp(np.cumsum(log_returns)))
    bars = []
    for i, c in enumerate(closes):
        bar = {
            "date": f"2024-01-{(i % 28) + 1:02d}",
            "high": c * 1.005,
            "low": c * 0.995,
            "close": c,
        }
        if with_volume:
            bar["volume"] = float(rng.lognormal(15, 0.3))
        bars.append(bar)
    return bars


def test_default_mode_uses_sma_adx_gate():
    bars = _bars(n=250)
    regime = classify_spy_regime(bars)  # mode defaults to "sma_adx"
    assert regime.state in ("GO", "NEUTRAL", "STAND_DOWN")
    assert hasattr(regime, "adx")
    assert regime.adx != 0.0 or regime.state == "NEUTRAL"  # sma_adx gate actually computes ADX


def test_hmm_mode_uses_hmm_gate():
    bars = _bars(n=250)
    regime = classify_spy_regime(bars, mode="hmm")
    assert regime.state in ("GO", "NEUTRAL", "STAND_DOWN")
    assert regime.adx == 0.0  # regime_gate_hmm.Regime always reports adx=0.0 (see its docstring)
    assert regime.n_states >= 0


def test_hmm_mode_uses_volume_when_present():
    """Indirect check: classify_spy_regime should pass volumes through
    without raising, and the underlying features should end up 3-wide."""
    from regime_gate_hmm import _build_features

    bars = _bars(n=250, with_volume=True)
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    features, _ = _build_features(closes, volumes=volumes)
    assert features.shape[1] == 3


def test_hmm_mode_falls_back_gracefully_without_volume():
    bars = _bars(n=250, with_volume=False)
    regime = classify_spy_regime(bars, mode="hmm")  # must not raise
    assert regime.state in ("GO", "NEUTRAL", "STAND_DOWN")


def test_unknown_mode_falls_back_to_sma_adx():
    bars = _bars(n=250)
    regime = classify_spy_regime(bars, mode="not-a-real-mode")
    assert regime.adx != 0.0 or regime.state == "NEUTRAL"  # sma_adx path, same as default
