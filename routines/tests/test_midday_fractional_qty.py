"""Tests for fractional-qty handling in midday_review's profit-exit logic.

Regression: the repair/review loops computed `qty = int(float(p.qty))`,
which truncates any fractional position (e.g. 0.08 shares, from a
notional/fractional buy) to 0. That fed into the "repair: ensure every
position has a stop order" loop's `if qty < 1: continue` guard, silently
skipping protection re-attach for every fractional position forever — the
opposite of what re-enabling fractional buys needs. profit_exit_action()
itself is pure logic and safe to test directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from midday_review import profit_exit_action


def test_fractional_qty_below_threshold_returns_none():
    assert profit_exit_action(pnl_pct=2.0, qty=0.08, threshold_pct=0.06) is None


def test_fractional_qty_above_threshold_returns_none_not_trim_or_full():
    # No profit-taking rule is defined yet for fractional qty (can't cleanly
    # trim or treat as a single share) — must return None, not crash or
    # misclassify as "trim" (which assumes qty >= 2 whole shares).
    assert profit_exit_action(pnl_pct=10.0, qty=0.08, threshold_pct=0.06) is None


def test_whole_share_qty_unaffected():
    assert profit_exit_action(pnl_pct=10.0, qty=1, threshold_pct=0.06) == "full"
    assert profit_exit_action(pnl_pct=10.0, qty=3, threshold_pct=0.06) == "trim"
