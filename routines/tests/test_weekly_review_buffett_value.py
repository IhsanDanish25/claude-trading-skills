"""Tests for weekly_review's Buffett Value weekly better-opportunity check.

market_close.py's daily _run_buffett_value_exits() deliberately omits a
candidate_pool (too expensive to re-screen ~100 symbols every day for a
low-turnover, buy-and-hold strategy), which means the "better opportunity
elsewhere" exit reason can never fire from the daily path alone.
_run_buffett_value_weekly_check() closes that gap on a weekly cadence by
screening fresh and delegating to the same execution path in
routines/market_close.py -- these tests cover that wiring, not the
execution logic itself (already covered by
test_market_close_buffett_value.py).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for the `routines.` package import

import weekly_review

# Import market_close the SAME way weekly_review.py's internal
# `from routines.market_close import ...` does -- package-qualified, not
# the bare `import market_close` other routines/tests/*.py files use.
# Python caches modules by their full qualified import path, so a bare
# import and a package-qualified import of the same file are two distinct
# module objects in sys.modules; monkeypatching one doesn't affect the
# other. weekly_review._run_buffett_value_weekly_check() only ever sees
# the routines.market_close instance, so tests must patch that one.
from routines import market_close


def test_skips_screen_entirely_when_nothing_tracked(monkeypatch):
    monkeypatch.setattr(weekly_review, "buffett_all", lambda: {})

    def _exploding_screen(*a, **k):
        raise AssertionError("screen_for_buffett_candidates must not run with nothing tracked")

    monkeypatch.setattr(weekly_review, "screen_for_buffett_candidates", _exploding_screen)

    broker = SimpleNamespace()  # must never be touched

    exited = weekly_review._run_buffett_value_weekly_check(broker)

    assert exited == 0


def test_screens_and_delegates_to_market_close_exits_with_fresh_pool(monkeypatch):
    monkeypatch.setattr(weekly_review, "buffett_all", lambda: {
        "CMCSA": {"entry_price": 26.18, "qty": 10, "entry_snapshot": {}}
    })

    fresh_pool = [{"symbol": "MSFT", "conviction_score": 0.95}]
    screened_universe = []
    monkeypatch.setattr(weekly_review, "screen_for_buffett_candidates",
                         lambda universe: (screened_universe.extend(universe), fresh_pool)[1])

    calls = []

    def _fake_exits(broker, candidate_pool=None):
        calls.append((broker, candidate_pool))
        return 1

    monkeypatch.setattr(market_close, "_run_buffett_value_exits", _fake_exits)

    broker = SimpleNamespace()
    exited = weekly_review._run_buffett_value_weekly_check(broker)

    assert exited == 1
    assert len(calls) == 1
    assert calls[0][0] is broker
    assert calls[0][1] == fresh_pool
    assert len(screened_universe) > 0  # a real universe was passed to the screen


def test_universe_filter_drops_non_ticker_shaped_entries(monkeypatch):
    """Defensive filter, matching market_open.py's entry handler: a stray
    non-ticker-shaped string in SP80_UNIVERSE (e.g. a section-header
    comment that lost its '#' and leaked in as a multi-word phrase, or an
    empty string) shouldn't reach the screen and waste an API call on
    something that can't resolve as a symbol.

    Note this filter can only catch entries that AREN'T ticker-shaped -- it
    cannot distinguish a real ticker from a plausible-looking fake one
    (e.g. the actual "REIT" bug this codebase had: 4 letters, all alpha,
    indistinguishable in shape from a real ticker). That one was only
    fixable at the source, see test_sp80_universe_no_longer_contains_stray_reit_entry
    below -- this test covers what the shape filter can actually catch."""
    monkeypatch.setattr(weekly_review, "buffett_all", lambda: {"CMCSA": {"entry_price": 1, "qty": 1}})
    monkeypatch.setattr(weekly_review.config, "SP80_UNIVERSE",
                         ["AAPL", "Real Estate", "", "TOOLONGFORATICKER", "MSFT"])

    seen_universe = []

    def _capture_universe(universe):
        seen_universe.extend(universe)
        return []

    monkeypatch.setattr(weekly_review, "screen_for_buffett_candidates", _capture_universe)
    monkeypatch.setattr(market_close, "_run_buffett_value_exits", lambda broker, candidate_pool=None: 0)

    weekly_review._run_buffett_value_weekly_check(SimpleNamespace())

    assert seen_universe == ["AAPL", "MSFT"]


def test_sp80_universe_no_longer_contains_stray_reit_entry():
    """Regression: core/config.py's SP80_UNIVERSE had a literal "REIT"
    string sitting where a "# Real Estate" section-header comment was
    clearly meant to go (right before the "# Communication" header) --
    every strategy that screens this universe, not just Buffett Value, was
    wasting a call on a symbol that isn't a real ticker."""
    from core.config import SP80_UNIVERSE

    assert "REIT" not in SP80_UNIVERSE
