"""Regression test for the backtest sizing-reproducibility bug.

run_earnings_simulation() used to read MAX_POSITION_SIZE_PCT/MAX_OPEN_POSITIONS
live from core.config — the LIVE trading account's sizing knobs. A local,
gitignored .env tuned for a $268 account (MAX_POSITION_SIZE_PCT=0.20,
MAX_OPEN_POSITIONS=4) silently changed a $100k backtest simulation's results:
the identical `validate_pead.py` invocation went from 158 trades (2026-07-05,
code defaults active) to 71 trades (2026-08-06, .env active) with no change
to the strategy logic, data, or CLI args. Backtests must be reproducible
independent of whatever the live bot's config happens to be at run time.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backtest_harness import earnings_engine


def test_sizing_defaults_are_fixed_backtest_constants_not_live_config():
    """The specific regression: the function's default sizing must come from
    this module's own fixed constants (0.05 / 10), not from whatever
    core.config.MAX_POSITION_SIZE_PCT / MAX_OPEN_POSITIONS currently is —
    those are live-trading knobs that a local .env can set to anything
    (e.g. 0.20 / 4 for a small account) and previously leaked straight into
    every backtest run."""
    sig = inspect.signature(earnings_engine.run_earnings_simulation)
    assert sig.parameters["max_position_pct"].default == earnings_engine.BACKTEST_MAX_POSITION_PCT
    assert sig.parameters["max_open_positions"].default == earnings_engine.BACKTEST_MAX_OPEN_POSITIONS
    # Pin the actual numeric values too — a change here should be a deliberate,
    # reviewed decision about backtest methodology, not a silent side effect.
    assert earnings_engine.BACKTEST_MAX_POSITION_PCT == 0.05
    assert earnings_engine.BACKTEST_MAX_OPEN_POSITIONS == 10
