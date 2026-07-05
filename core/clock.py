"""Backtest-aware clock.

Live trading: today() returns the real wall-clock date (unchanged behavior).
Backtesting: the engine calls set_today(AS_OF) each simulated day so screeners
that reason about recency/age ("reported 8-45 days ago", "insider bought in the
last 14 days") compute those windows relative to the simulated date instead of
real today — otherwise every historical event reads as "too old" and gets
filtered out.

Screeners should call clock.today() instead of datetime.date.today() wherever
the value participates in an age/recency decision. Default (AS_OF unset) is a
drop-in for datetime.date.today().
"""
from __future__ import annotations

import datetime

_AS_OF: datetime.date | None = None


def set_today(d: datetime.date | None) -> None:
    """Override 'today' (backtest). Pass None to clear."""
    global _AS_OF
    _AS_OF = d


def reset() -> None:
    """Restore live behavior (today() == real wall-clock date)."""
    global _AS_OF
    _AS_OF = None


def today() -> datetime.date:
    return _AS_OF if _AS_OF is not None else datetime.date.today()


def is_backtest() -> bool:
    """True when an AS_OF override is active (i.e. running inside the engine).
    Screeners use this to skip live-only side effects like daily disk caching."""
    return _AS_OF is not None
