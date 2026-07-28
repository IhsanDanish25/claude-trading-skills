"""Unit tests for midday_review.profit_exit_action — the review-time
profit-taking decision.

Regression for the single-share profit gap (2026-07-28): a 1-share position
hit its target but the 50%-trim block required qty>=2, so it never
auto-realized profit and only the trailing stop / Claude SELL / hold-limit
could exit it. The "full" branch closes that gap.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from midday_review import profit_exit_action

THRESH = 0.06  # PARTIAL_PROFIT_PCT default (+6%)


def test_below_threshold_returns_none():
    assert profit_exit_action(5.9, 1, THRESH) is None
    assert profit_exit_action(5.9, 5, THRESH) is None


def test_multi_share_at_threshold_trims():
    assert profit_exit_action(6.0, 2, THRESH) == "trim"
    assert profit_exit_action(9.9, 10, THRESH) == "trim"


def test_single_share_at_threshold_full_exits():
    # The fix: 1-share winners realize the whole position instead of being skipped.
    assert profit_exit_action(6.0, 1, THRESH) == "full"
    assert profit_exit_action(7.22, 1, THRESH) == "full"  # CNC's actual +7.22%


def test_zero_qty_is_noop():
    assert profit_exit_action(10.0, 0, THRESH) is None


def test_exact_boundary_is_inclusive():
    assert profit_exit_action(6.0, 1, THRESH) == "full"
    assert profit_exit_action(5.999, 1, THRESH) is None
