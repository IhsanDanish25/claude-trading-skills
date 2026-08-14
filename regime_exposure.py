"""
regime_exposure.py — regime-based target exposure suggestion (ADVISORY ONLY).

Maps regime_gate_hmm.classify()'s detected state to a target portfolio
exposure percentage, using the same 5-bucket scheme (crash / bear / neutral
/ bull / euphoria) as the regime-trader side project's core.regime_strategies
module. This is purely advisory: it computes and can log a suggested
exposure, but nothing in the live trading path reads or acts on it yet.
Nothing here changes what any routine actually does - see routines/
market_open.py's shadow-log call site.

Status: unproven live, same caveat as regime_gate_hmm.py. Ported concept,
not a literal copy - regime-trader's own version is tied to its own
Alpaca-wrapper/backtest structure that doesn't exist in this codebase.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("regime_exposure")

# bucket -> (target exposure %, max leverage, trailing stop %, bias)
EXPOSURE_RULES = {
    "crash": {
        "exposure": 0.00,
        "max_leverage": 1.00,
        "trailing_stop_pct": None,
        "bias": "cash_treasury",
    },
    "bear": {
        "exposure": 0.25,
        "max_leverage": 1.00,
        "trailing_stop_pct": 0.05,
        "bias": "defensive_market_neutral",
    },
    "neutral": {
        "exposure": 0.60,
        "max_leverage": 1.00,
        "trailing_stop_pct": 0.08,
        "bias": "trend_following",
    },
    "bull": {
        "exposure": 1.00,
        "max_leverage": 1.25,
        "trailing_stop_pct": 0.10,
        "bias": "full_trend",
    },
    "euphoria": {
        "exposure": 0.70,
        "max_leverage": 1.00,
        "trailing_stop_pct": 0.04,
        "bias": "tighten_stops",
    },
}

_BUCKET_PALETTE = ["crash", "bear", "neutral", "bull", "euphoria"]


def bucket_for_state(state_idx: int, n_states: int) -> str:
    """Map an HMM state index (0 = coldest/lowest-return) onto one of the 5
    exposure buckets, evenly spacing across the palette when n_states < 5
    (mirrors regime-trader's core.hmm_engine._regime_label_palette: with
    fewer states, the model can't distinguish the middle buckets, so extremes
    get pulled toward "crash"/"euphoria" rather than guessing a false middle).
    """
    if n_states <= 1 or state_idx < 0:
        return "neutral"
    scaled_pos = round(state_idx * (len(_BUCKET_PALETTE) - 1) / (n_states - 1))
    return _BUCKET_PALETTE[scaled_pos]


@dataclass
class ExposureSuggestion:
    bucket: str
    target_exposure_pct: float
    max_leverage: float
    trailing_stop_pct: Optional[float]
    bias: str
    reason: str


def suggest_exposure(regime) -> ExposureSuggestion:
    """`regime` is a regime_gate_hmm.Regime (from classify()). Returns the
    suggested target exposure for its detected state - advisory only, does
    not gate or size anything on its own. Low confidence or a missing
    state_idx (any fallback path) always resolves to "neutral".
    """
    if regime.state_idx < 0 or regime.n_states <= 0:
        bucket = "neutral"
    else:
        bucket = bucket_for_state(regime.state_idx, regime.n_states)

    rule = EXPOSURE_RULES[bucket]
    return ExposureSuggestion(
        bucket=bucket,
        target_exposure_pct=rule["exposure"],
        max_leverage=rule["max_leverage"],
        trailing_stop_pct=rule["trailing_stop_pct"],
        bias=rule["bias"],
        reason=(
            f"HMM bucket '{bucket}' from state {regime.state_idx}/{regime.n_states}, "
            f"confidence {regime.confidence:.0%}"
        ),
    )
