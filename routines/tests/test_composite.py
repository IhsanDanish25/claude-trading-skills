"""Unit tests for the composite scoring engine (core/composite.py).

Pure-logic tests — no Alpaca / FMP / network. GROUP B scorers are exercised
with COMPOSITE_USE_FMP disabled so they return the neutral 50 deterministically.
"""
import math
import os

os.environ.setdefault("COMPOSITE_USE_FMP", "false")

from core import composite as cp  # noqa: E402


def _make_bars(start=60.0, n=210, drift=0.4, vol=2_000_000):
    """Synthetic daily bars, newest-first (as core.screener produces)."""
    bars, px = [], start
    for i in range(n):
        px *= (1 + drift / 100)
        bars.append({"close": px, "high": px * 1.01, "low": px * 0.99,
                     "volume": vol * (1 + 0.3 * math.sin(i / 5))})
    bars.reverse()
    return bars


STRONG = {"symbol": "NVDA", "price": 120, "rel_volume": 2.1, "adr_pct": 2.0,
          "contraction_weeks": 3, "tight_closes": 4, "pct_from_52w_high": -1.5,
          "near_52w_high": True, "rs_vs_spy": 14.0, "gap_pct": 1.2,
          "raw_score": 95, "score": 95}
WEAK = {"symbol": "COCO", "price": 30, "rel_volume": 0.9, "adr_pct": 4.0,
        "contraction_weeks": 0, "tight_closes": 1, "pct_from_52w_high": -18.0,
        "near_52w_high": False, "rs_vs_spy": -4.0, "gap_pct": 0.1,
        "raw_score": 20, "score": 20}
CTX = {"regime_score": 78.0, "regime_mult": 0.901, "sector_mom": {"XLK": 88.0, "XLP": 35.0}}


def test_weights_sum_to_one():
    assert abs(sum(cp.WEIGHTS.values()) - 1.0) < 1e-9


def test_strong_outranks_weak():
    s = cp.compute_composite(STRONG, _make_bars(drift=0.5), CTX)
    w = cp.compute_composite(WEAK, _make_bars(drift=-0.2, vol=400_000), CTX)
    assert s["final"] > w["final"]
    assert 0 <= s["final"] <= 100
    assert 0 <= w["final"] <= 100


def test_group_b_neutral_when_fmp_disabled():
    r = cp.compute_composite(STRONG, _make_bars(), CTX)
    assert r["breakdown"]["earnings"]["raw"] == 50.0
    assert r["breakdown"]["fundamental"]["raw"] == 50.0
    assert r["breakdown"]["earnings"]["group"] == "B"


def test_missing_bars_neutral_no_crash():
    r = cp.compute_composite(STRONG, None, CTX)
    assert r["breakdown"]["trend"]["raw"] == 50.0
    assert r["breakdown"]["momentum"]["raw"] == 50.0
    assert r["final"] > 0


def test_all_subscores_present_and_bounded():
    r = cp.compute_composite(STRONG, _make_bars(), CTX)
    assert set(r["breakdown"]) == set(cp.WEIGHTS)
    for name, d in r["breakdown"].items():
        assert 0 <= d["raw"] <= 100, name
        assert d["group"] in ("A", "B")


def test_rs_scorer_monotonic():
    assert cp.score_rs({"rs_vs_spy": 20}) > cp.score_rs({"rs_vs_spy": 0})
    assert cp.score_rs({"rs_vs_spy": None}) == cp.NEUTRAL


def test_regime_multiplier_scales_final():
    bars = _make_bars()
    hot = cp.compute_composite(STRONG, bars, {**CTX, "regime_mult": 1.0})
    cold = cp.compute_composite(STRONG, bars, {**CTX, "regime_mult": 0.55})
    assert hot["final"] > cold["final"]
    assert hot["composite"] == cold["composite"]  # raw composite unchanged by regime


def test_format_breakdown_renders():
    r = cp.compute_composite(STRONG, _make_bars(), CTX)
    text = cp.format_breakdown(r)
    assert "COMPOSITE NVDA" in text
    assert "vcp" in text and "[B] earnings" in text
