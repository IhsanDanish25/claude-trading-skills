"""
regime_gate_hmm.py — causal Hidden Markov Model regime classifier.

Opt-in DROP-IN alternative to regime_gate.classify(), gated by
REGIME_GATE_MODE=hmm (default stays "sma_adx" -> the existing, proven gate is
untouched and still the default). Same call shape, same Regime vocabulary
(GO / NEUTRAL / STAND_DOWN, .can_trade), same (highs, lows, closes,
oldest -> newest) argument order as regime_gate.classify(), so market_open.py
only needs a one-line branch to use it - see routines/market_open.py.

Why this exists: SMA50-vs-SMA200 + ADX (regime_gate.py) is a fast, robust
two-state trend/chop filter. This is a richer alternative that fits a
Gaussian HMM over daily log-returns + realized volatility, picking the
number of states (3-5) by BIC rather than hardcoding two, and inferring
today's regime with the forward algorithm ONLY (never hmmlearn's
predict()/predict_proba(), which run forward-BACKWARD smoothing and would
leak future bars into today's read) - see _forward_log_alpha below. A
stability filter requires a state to hold for CONFIRMATION_BARS consecutive
days before the ACTIVE regime switches, and a low-confidence day falls back
to NEUTRAL rather than trusting a shaky GO/STAND_DOWN call.

Status: unproven live. Ported from a separate side-project's backtested (not
live-traded) regime engine. Run it side-by-side in logs (REGIME_GATE_MODE
unset) before ever flipping it to actually gate real trades.
"""

import logging
import math
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger("regime_gate_hmm")

try:
    from hmmlearn.hmm import GaussianHMM

    _HMMLEARN_AVAILABLE = True
except ImportError:
    GaussianHMM = None
    _HMMLEARN_AVAILABLE = False

MIN_STATES = 3
MAX_STATES = 5
MIN_TRAIN_SAMPLES = 150  # below this, fall back to NEUTRAL rather than fitting on too little data
REALIZED_VOL_WINDOW = 10
CONFIRMATION_BARS = 3  # bars a new state must persist before the active regime switches
MIN_CONFIDENCE = 0.60  # below this, downgrade the day's call to NEUTRAL regardless of state


@dataclass
class Regime:
    """Same shape as regime_gate.Regime, plus confidence/n_states for logging."""

    state: str  # "GO" | "NEUTRAL" | "STAND_DOWN"
    trend: str  # "up" | "down" | "flat" (diagnostic, from SMA50 vs SMA200)
    adx: (
        float  # always 0.0 here - kept only for log-line / dataclass parity with regime_gate.Regime
    )
    sma50: float
    sma200: float
    reason: str
    confidence: float = 0.0
    n_states: int = 0

    @property
    def can_trade(self) -> bool:
        return self.state in ("GO", "NEUTRAL")


def _sma(values: list[float], n: int) -> float:
    if len(values) < n:
        return sum(values) / len(values)
    return sum(values[-n:]) / n


def _neutral_fallback(closes: list[float], reason: str) -> Regime:
    sma50 = _sma(closes, 50) if closes else 0.0
    sma200 = _sma(closes, 200) if closes else 0.0
    trend = "up" if sma50 > sma200 else "down" if sma50 < sma200 else "flat"
    return Regime("NEUTRAL", trend, 0.0, sma50, sma200, reason, confidence=0.0, n_states=0)


def _build_features(closes: list[float]) -> tuple[np.ndarray, list[float]]:
    """log-return + REALIZED_VOL_WINDOW-day annualized realized vol, causal
    (each row uses only closes up to and including that day)."""
    closes_arr = np.asarray(closes, dtype=float)
    log_returns = np.diff(
        np.log(closes_arr)
    )  # len = n-1, log_returns[i] uses closes[i], closes[i+1]

    n = len(log_returns)
    vol = np.full(n, np.nan)
    for i in range(REALIZED_VOL_WINDOW - 1, n):
        window = log_returns[i - REALIZED_VOL_WINDOW + 1 : i + 1]
        vol[i] = window.std(ddof=1) * math.sqrt(252)

    valid = ~np.isnan(vol)
    features = np.column_stack([log_returns[valid], vol[valid]])
    # features[i] corresponds to closes[i + 1 + first_valid_offset]; return the aligned close price list too
    valid_closes = [closes[i + 1] for i in range(n) if valid[i]]
    return features, valid_closes


def _emission_log_prob(model, X: np.ndarray) -> np.ndarray:
    """Per-state Gaussian log-likelihood; only ever depends on the observation
    at that row, so batching this over many rows leaks nothing across rows."""
    from scipy.stats import multivariate_normal

    n_components = model.means_.shape[0]
    log_prob = np.zeros((len(X), n_components))
    for i in range(n_components):
        cov = model.covars_[i] if model.covariance_type == "full" else np.diag(model.covars_[i])
        log_prob[:, i] = multivariate_normal(
            mean=model.means_[i], cov=cov, allow_singular=True
        ).logpdf(X)
    return log_prob


def _forward_log_alpha(model, X: np.ndarray) -> np.ndarray:
    """Forward algorithm ONLY (never hmmlearn's predict/predict_proba, which
    smooth with future observations). log_alpha[t] depends only on
    log_alpha[t-1] and the observation AT t - changing/appending rows after t
    cannot change the result at t."""
    from scipy.special import logsumexp

    log_B = _emission_log_prob(model, X)
    log_startprob = np.log(np.clip(model.startprob_, 1e-300, None))
    log_transmat = np.log(np.clip(model.transmat_, 1e-300, None))

    log_alpha = np.zeros_like(log_B)
    log_alpha[0] = log_startprob + log_B[0]
    for t in range(1, len(X)):
        log_alpha[t] = logsumexp(log_alpha[t - 1][:, None] + log_transmat, axis=0) + log_B[t]
    return log_alpha


def _fit_best_model(
    X: np.ndarray,
    min_states: int,
    max_states: int,
    n_fit_attempts: int,
    n_iter: int,
    random_state: int,
):
    """Fit candidate GaussianHMMs for min_states..max_states, pick the lowest BIC."""
    best_model, best_bic = None, math.inf
    n_samples, n_features = X.shape

    for n_states in range(min_states, max_states + 1):
        if n_samples < n_states * 2:
            continue
        candidate, candidate_ll = None, -math.inf
        for attempt in range(n_fit_attempts):
            try:
                model = GaussianHMM(
                    n_components=n_states,
                    covariance_type="full",
                    n_iter=n_iter,
                    random_state=random_state + attempt,
                )
                model.fit(X)
                ll = model.score(X)
            except Exception as exc:
                logger.warning(
                    "HMM fit failed for n_states=%d (attempt %d): %s", n_states, attempt, exc
                )
                continue
            if ll > candidate_ll:
                candidate, candidate_ll = model, ll
        if candidate is None:
            continue

        n_params = (
            (n_states - 1)
            + n_states * (n_states - 1)
            + n_states * n_features
            + n_states * n_features * (n_features + 1) // 2
        )
        bic = -2 * candidate_ll + n_params * math.log(n_samples)
        if bic < best_bic:
            best_model, best_bic = candidate, bic

    return best_model


def _reorder_states_by_return(model) -> None:
    """Relabel states 0..N-1 by ascending mean log-return (feature 0), in place."""
    perm = np.argsort(model.means_[:, 0])
    model.means_ = model.means_[perm]
    model.covars_ = model.covars_[perm]
    model.startprob_ = model.startprob_[perm]
    model.transmat_ = model.transmat_[perm][:, perm]


def _state_to_gate(state_idx: int, n_states: int) -> str:
    """Lowest-return state -> STAND_DOWN, highest-return state -> GO, everything in between -> NEUTRAL."""
    if state_idx == 0:
        return "STAND_DOWN"
    if state_idx == n_states - 1:
        return "GO"
    return "NEUTRAL"


def _apply_stability_filter(raw_states: list[int], confirmation_bars: int) -> list[int]:
    """Require a new state to persist `confirmation_bars` consecutive days
    before the active state switches - causal (day t only uses days <= t)."""
    active = raw_states[0]
    candidate, candidate_count = None, 0
    filtered = []
    for raw in raw_states:
        if raw == active:
            candidate, candidate_count = None, 0
        else:
            if raw == candidate:
                candidate_count += 1
            else:
                candidate, candidate_count = raw, 1
            if candidate_count >= confirmation_bars:
                active, candidate, candidate_count = candidate, None, 0
        filtered.append(active)
    return filtered


def classify(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float] | None = None,
    min_states: int = MIN_STATES,
    max_states: int = MAX_STATES,
    n_fit_attempts: int = 3,
    n_iter: int = 200,
    random_state: int = 42,
) -> Regime:
    """Drop-in for regime_gate.classify(highs, lows, closes) - same arg order
    (oldest -> newest), same Regime.can_trade contract. `volumes` is accepted
    for API symmetry but not currently used as a feature.
    """
    if not _HMMLEARN_AVAILABLE:
        return _neutral_fallback(closes, "hmmlearn not installed -> neutral (see requirements.txt)")

    if len(closes) < MIN_TRAIN_SAMPLES:
        return _neutral_fallback(
            closes,
            f"insufficient history ({len(closes)} bars, need {MIN_TRAIN_SAMPLES}) -> neutral",
        )

    features, aligned_closes = _build_features(closes)
    if len(features) < MIN_TRAIN_SAMPLES:
        return _neutral_fallback(closes, "insufficient post-warm-up history for HMM -> neutral")

    model = _fit_best_model(features, min_states, max_states, n_fit_attempts, n_iter, random_state)
    if model is None:
        return _neutral_fallback(
            closes, "HMM fit failed for every candidate state count -> neutral"
        )

    _reorder_states_by_return(model)
    n_states = model.n_components

    log_alpha = _forward_log_alpha(model, features)
    row_max = log_alpha.max(axis=1, keepdims=True)
    filtered_proba = np.exp(log_alpha - row_max)
    filtered_proba /= filtered_proba.sum(axis=1, keepdims=True)

    raw_states = list(np.argmax(filtered_proba, axis=1))
    confirmed_states = _apply_stability_filter(raw_states, CONFIRMATION_BARS)

    today_confidence = float(filtered_proba[-1].max())
    today_state_idx = confirmed_states[-1]
    gate = _state_to_gate(today_state_idx, n_states)

    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    trend = "up" if sma50 > sma200 else "down" if sma50 < sma200 else "flat"

    if today_confidence < MIN_CONFIDENCE:
        return Regime(
            "NEUTRAL",
            trend,
            0.0,
            sma50,
            sma200,
            f"HMM state {today_state_idx}/{n_states} confidence {today_confidence:.0%} below "
            f"{MIN_CONFIDENCE:.0%} floor -> neutral (would have been {gate})",
            confidence=today_confidence,
            n_states=n_states,
        )

    return Regime(
        gate,
        trend,
        0.0,
        sma50,
        sma200,
        f"HMM state {today_state_idx}/{n_states} (0=coldest, {n_states - 1}=hottest), "
        f"confidence {today_confidence:.0%}",
        confidence=today_confidence,
        n_states=n_states,
    )
