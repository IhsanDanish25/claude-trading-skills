"""Regression: the sma_adx regime gate's STAND_DOWN state used to `return`
out of market_open.run() entirely, silencing every strategy in
STRATEGY_MODE -- not just the trend-following ones it was designed for
(the module's own log message says "VCP momentum stands down", and
regime_gate.py's docstring says "VCP momentum bleeds money in choppy/
ranging markets"). That meant meanrev, insider, earnmom and pead never
got a chance to screen or buy on any day SPY's ADX sat under the ranging
threshold -- which live logs showed happening for two straight weeks,
looking like the bot had stopped buying entirely.

STAND_DOWN must now only skip the trend-following strategies
(_TREND_GATED_STRATEGIES); event-driven/countertrend strategies must
still run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_open


def test_trend_following_strategies_are_skipped_on_standdown():
    for strategy in market_open._TREND_GATED_STRATEGIES:
        assert market_open._should_skip_for_regime(strategy, regime_standdown=True) is True


def test_event_driven_strategies_still_run_on_standdown():
    """The actual bug: meanrev/insider/earnmom/pead must NOT be silenced
    by a gate that exists to protect trend-following VCP/breakout entries."""
    for strategy in ("meanrev", "insider", "earnmom", "pead", "buffett_value",
                      "crypto", "sector"):
        assert market_open._should_skip_for_regime(strategy, regime_standdown=True) is False


def test_nothing_is_skipped_when_regime_is_not_standing_down():
    all_strategies = set(market_open.STRATEGY_HANDLERS.keys())
    for strategy in all_strategies:
        assert market_open._should_skip_for_regime(strategy, regime_standdown=False) is False


def test_every_trend_gated_strategy_has_a_registered_handler():
    """Guards against the gate silently no-oping if a strategy name in
    _TREND_GATED_STRATEGIES is ever renamed/removed from STRATEGY_HANDLERS."""
    for strategy in market_open._TREND_GATED_STRATEGIES:
        assert strategy in market_open.STRATEGY_HANDLERS
