"""Unit tests for regime_exposure.py's advisory exposure mapping."""

import regime_exposure as re
import regime_gate_hmm as rgh


def _regime(state_idx=2, n_states=5, confidence=0.90, state="NEUTRAL"):
    return rgh.Regime(
        state=state,
        trend="up",
        adx=0.0,
        sma50=100.0,
        sma200=95.0,
        reason="test",
        confidence=confidence,
        n_states=n_states,
        state_idx=state_idx,
    )


# ---------------------------------------------------------------------------
# bucket_for_state
# ---------------------------------------------------------------------------


def test_bucket_for_state_full_five_state_palette():
    assert re.bucket_for_state(0, 5) == "crash"
    assert re.bucket_for_state(1, 5) == "bear"
    assert re.bucket_for_state(2, 5) == "neutral"
    assert re.bucket_for_state(3, 5) == "bull"
    assert re.bucket_for_state(4, 5) == "euphoria"


def test_bucket_for_state_three_state_extremes():
    # With only 3 states, the model can't resolve the "bear"/"bull" middle
    # buckets - extremes should map to the true extremes.
    assert re.bucket_for_state(0, 3) == "crash"
    assert re.bucket_for_state(1, 3) == "neutral"
    assert re.bucket_for_state(2, 3) == "euphoria"


def test_bucket_for_state_negative_index_is_neutral():
    assert re.bucket_for_state(-1, 5) == "neutral"


# ---------------------------------------------------------------------------
# suggest_exposure: matches the required exposure/leverage/stop table
# ---------------------------------------------------------------------------


def test_suggest_exposure_matches_regime_rules_table():
    cases = [
        (0, 0.00, 1.00, None),  # crash
        (1, 0.25, 1.00, 0.05),  # bear
        (2, 0.60, 1.00, 0.08),  # neutral
        (3, 1.00, 1.25, 0.10),  # bull
        (4, 0.70, 1.00, 0.04),  # euphoria
    ]
    for state_idx, exposure, leverage, stop in cases:
        suggestion = re.suggest_exposure(_regime(state_idx=state_idx, n_states=5))
        assert suggestion.target_exposure_pct == exposure
        assert suggestion.max_leverage == leverage
        assert suggestion.trailing_stop_pct == stop


def test_suggest_exposure_fallback_regime_is_neutral():
    fallback = _regime(state_idx=-1, n_states=0, confidence=0.0)
    suggestion = re.suggest_exposure(fallback)
    assert suggestion.bucket == "neutral"
    assert suggestion.target_exposure_pct == 0.60


def test_suggest_exposure_reason_includes_confidence_and_state():
    suggestion = re.suggest_exposure(_regime(state_idx=3, n_states=5, confidence=0.82))
    assert "3/5" in suggestion.reason
    assert "82%" in suggestion.reason
