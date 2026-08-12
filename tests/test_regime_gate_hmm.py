"""Unit tests for regime_gate_hmm.py's causal HMM regime classifier.

Mirrors tests/test_regime_gate.py's synthetic-bar approach. HMM fits are kept
fast for tests via small state ranges / low n_iter (see _fast_kwargs).
"""

import numpy as np

import regime_gate_hmm as rgh

# Fast-fit knobs so the test suite doesn't pay production-grade HMM fit cost.
_fast_kwargs = dict(min_states=3, max_states=3, n_fit_attempts=1, n_iter=50)


def _bars(
    n=300,
    seed=7,
    regime_len=60,
    mu_calm=0.0006,
    sigma_calm=0.006,
    mu_stress=-0.0012,
    sigma_stress=0.028,
):
    """Synthetic daily OHLC alternating calm/uptrend and stressed/downtrend
    regimes every `regime_len` bars, oldest -> newest (matches fetch_bars'
    convention as consumed by market_open.py's classify() call site)."""
    rng = np.random.default_rng(seed)
    log_returns = np.empty(n)
    pos, calm = 0, True
    while pos < n:
        length = min(regime_len, n - pos)
        if calm:
            log_returns[pos : pos + length] = rng.normal(mu_calm, sigma_calm, length)
        else:
            log_returns[pos : pos + length] = rng.normal(mu_stress, sigma_stress, length)
        calm = not calm
        pos += length

    closes = list(100 * np.exp(np.cumsum(log_returns)))
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    return highs, lows, closes


def _volumes(n=300, seed=11):
    rng = np.random.default_rng(seed)
    return list(rng.lognormal(mean=15, sigma=0.3, size=n))


def test_insufficient_history_falls_back_to_neutral():
    highs, lows, closes = _bars(n=50)
    regime = rgh.classify(highs, lows, closes)
    assert regime.state == "NEUTRAL"
    assert regime.can_trade is True
    assert "insufficient history" in regime.reason


def test_hmmlearn_unavailable_falls_back_to_neutral(monkeypatch):
    highs, lows, closes = _bars()
    monkeypatch.setattr(rgh, "_HMMLEARN_AVAILABLE", False)
    regime = rgh.classify(highs, lows, closes)
    assert regime.state == "NEUTRAL"
    assert "hmmlearn not installed" in regime.reason


def test_classify_returns_valid_regime_shape():
    highs, lows, closes = _bars()
    regime = rgh.classify(highs, lows, closes, **_fast_kwargs)
    assert regime.state in ("GO", "NEUTRAL", "STAND_DOWN")
    assert regime.trend in ("up", "down", "flat")
    assert 0.0 <= regime.confidence <= 1.0
    assert regime.n_states == 3
    assert regime.can_trade == (regime.state != "STAND_DOWN")
    assert 0 <= regime.state_idx < regime.n_states


def test_state_idx_matches_state_gate():
    """state_idx should always be internally consistent with the reported gate."""
    highs, lows, closes = _bars()
    regime = rgh.classify(highs, lows, closes, **_fast_kwargs)
    if regime.state_idx == 0:
        assert regime.state in ("STAND_DOWN", "NEUTRAL")  # NEUTRAL if confidence-downgraded
    if regime.state_idx == regime.n_states - 1:
        assert regime.state in ("GO", "NEUTRAL")


def test_low_confidence_downgrades_to_neutral(monkeypatch):
    """Even if the raw HMM state would gate STAND_DOWN or GO, a confidence
    below MIN_CONFIDENCE must downgrade the day's call to NEUTRAL."""
    highs, lows, closes = _bars()

    # Force _state_to_gate to report STAND_DOWN (as if state 0 were active)
    # while patching the filtered-probability confidence path indirectly:
    # simplest deterministic approach is to raise MIN_CONFIDENCE above 1.0.
    monkeypatch.setattr(rgh, "MIN_CONFIDENCE", 1.01)
    regime = rgh.classify(highs, lows, closes, **_fast_kwargs)
    assert regime.state == "NEUTRAL"
    assert "below" in regime.reason
    assert regime.can_trade is True


def test_stability_filter_requires_confirmation_bars():
    # Flips to state 1 for only 2 bars (not enough to confirm) then back to 0.
    raw = [0, 0, 0, 1, 1, 0, 0, 0]
    assert rgh._apply_stability_filter(raw, confirmation_bars=3) == [0] * 8

    # Flips to state 1 and stays >= 3 bars: confirms the switch.
    raw2 = [0, 0, 0, 1, 1, 1, 1, 1]
    assert rgh._apply_stability_filter(raw2, confirmation_bars=3) == [0, 0, 0, 0, 0, 1, 1, 1]


def test_state_to_gate_extremes_and_middle():
    assert rgh._state_to_gate(0, n_states=5) == "STAND_DOWN"
    assert rgh._state_to_gate(4, n_states=5) == "GO"
    for middle in (1, 2, 3):
        assert rgh._state_to_gate(middle, n_states=5) == "NEUTRAL"


def test_features_are_causal_no_lookahead():
    """Filtered probabilities at row t must be identical whether computed
    from the full close series or a series truncated right after t - the
    forward algorithm must never be affected by future closes."""
    highs, lows, closes = _bars(n=300)
    features, _ = rgh._build_features(closes)

    model = rgh._fit_best_model(features, **{**_fast_kwargs, "random_state": 42})
    rgh._reorder_states_by_return(model)

    full_log_alpha = rgh._forward_log_alpha(model, features)
    cut = len(features) // 2
    truncated_log_alpha = rgh._forward_log_alpha(model, features[: cut + 1])

    np.testing.assert_allclose(full_log_alpha[: cut + 1], truncated_log_alpha, atol=1e-6)


def test_build_features_shapes_and_alignment():
    highs, lows, closes = _bars(n=100)
    features, aligned_closes = rgh._build_features(closes)
    assert features.shape[1] == 2  # log_return, realized_vol
    assert len(features) == len(aligned_closes)
    assert np.isfinite(features).all()


# ---------------------------------------------------------------------------
# Volume feature (opt-in via the `volumes` argument)
# ---------------------------------------------------------------------------


def test_build_features_without_volumes_stays_two_dimensional():
    _, _, closes = _bars(n=100)
    features, _ = rgh._build_features(closes, volumes=None)
    assert features.shape[1] == 2


def test_build_features_with_volumes_adds_third_column():
    _, _, closes = _bars(n=100)
    volumes = _volumes(n=100)
    features, aligned_closes = rgh._build_features(closes, volumes=volumes)
    assert features.shape[1] == 3
    assert len(features) == len(aligned_closes)
    assert np.isfinite(features).all()


def test_build_features_mismatched_volume_length_falls_back_gracefully():
    _, _, closes = _bars(n=100)
    features, _ = rgh._build_features(closes, volumes=_volumes(n=50))  # wrong length
    assert features.shape[1] == 2  # silently ignored, not an error


def test_volume_zscore_is_causal_and_windowed():
    volumes = _volumes(n=50)
    z = rgh._volume_zscore(volumes, window=20)
    assert np.isnan(z[:19]).all()  # not enough history yet
    assert np.isfinite(z[19:]).all()


def test_classify_with_volumes_returns_valid_regime():
    highs, lows, closes = _bars()
    volumes = _volumes(n=len(closes))
    regime = rgh.classify(highs, lows, closes, volumes=volumes, **_fast_kwargs)
    assert regime.state in ("GO", "NEUTRAL", "STAND_DOWN")
    assert 0 <= regime.state_idx < regime.n_states


def test_features_with_volumes_are_causal_no_lookahead():
    """Same no-lookahead guarantee as test_features_are_causal_no_lookahead,
    but with the volume feature included."""
    highs, lows, closes = _bars(n=300)
    volumes = _volumes(n=300)
    features, _ = rgh._build_features(closes, volumes=volumes)

    model = rgh._fit_best_model(features, **{**_fast_kwargs, "random_state": 42})
    rgh._reorder_states_by_return(model)

    full_log_alpha = rgh._forward_log_alpha(model, features)
    cut = len(features) // 2
    truncated_log_alpha = rgh._forward_log_alpha(model, features[: cut + 1])

    np.testing.assert_allclose(full_log_alpha[: cut + 1], truncated_log_alpha, atol=1e-6)
