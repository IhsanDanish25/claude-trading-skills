"""
core/trading_window.py

Replaces the fixed 2.0h stale-catch-up cap with logic anchored to Alpaca's 24/5
equities window (Sunday 20:00 ET through Friday 20:00 ET).

Problem this solves
-------------------
The scheduler ticks every 600s. If the worker container restarts more than 2.0h
after a routine's scheduled fire time, the routine is skipped entirely. On
2026-07-22 the container came up at 12:07 ET, past the 09:35 ET market_open
window, and ticks #2-#5 all logged "Skipping stale catch-up for
routines.market_open". market_open never ran that day. No trades were possible.

With the 24/5 window there is no longer a reason to discard the whole day. This
module answers two questions instead:

    1. Are we still inside a tradeable session for this routine's session date?
    2. Has this routine already run for that session date?

If yes and no, the catch-up is allowed regardless of how many hours late it is.

Live account only. Nothing here selects or routes to a simulated account.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Regular US equities session.
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

# Alpaca 24/5 equities window: Sunday 20:00 ET -> Friday 20:00 ET.
EXT_WEEK_OPEN_DAY = 6      # Sunday (Python weekday(): Mon=0 .. Sun=6)
EXT_WEEK_OPEN_TIME = time(20, 0)
EXT_WEEK_CLOSE_DAY = 4     # Friday
EXT_WEEK_CLOSE_TIME = time(20, 0)

# Routines that are allowed to run late via catch-up, and the last ET time on the
# session date past which catching up is pointless. Sized so a catch-up still has
# room to open and manage a position before the session ends.
CATCHUP_DEADLINES: dict[str, time] = {
    "routines.market_open": time(18, 0),
    "routines.midday_review": time(18, 0),
    "routines.pre_market": time(9, 30),
    # market_close and weekly_review are deliberately absent: running them late
    # is meaningless, they should still be skipped when stale.
}


def now_et() -> datetime:
    return datetime.now(tz=ET)


# --------------------------------------------------------------------------
# Session windows
# --------------------------------------------------------------------------

def is_regular_hours(now: datetime | None = None) -> bool:
    """True during 09:30-16:00 ET on a weekday. Does not account for holidays."""
    now = now or now_et()
    if now.weekday() > 4:
        return False
    return RTH_OPEN <= now.timetz().replace(tzinfo=None) < RTH_CLOSE


def is_extended_window(now: datetime | None = None) -> bool:
    """
    True inside Alpaca's 24/5 equities window.

    Opens Sunday 20:00 ET, closes Friday 20:00 ET, continuous in between.
    Holiday closures are NOT modelled here -- see is_tradeable() for that.
    """
    now = now or now_et()
    wd = now.weekday()
    t = now.timetz().replace(tzinfo=None)

    if wd == EXT_WEEK_OPEN_DAY:                    # Sunday
        return t >= EXT_WEEK_OPEN_TIME
    if wd == EXT_WEEK_CLOSE_DAY:                   # Friday
        return t < EXT_WEEK_CLOSE_TIME
    if wd == 5:                                    # Saturday
        return False
    return True                                    # Mon-Thu, continuous


def is_tradeable(now: datetime | None = None, calendar_check=None) -> bool:
    """
    Inside the 24/5 window AND not a market holiday.

    calendar_check: optional callable taking a date and returning True if the
    market is open that day. Pass the broker's calendar lookup here. If omitted,
    holidays are not detected and the function may return True on a closed day --
    acceptable only because order placement will fail harmlessly, but wire the
    calendar in before enabling autonomous catch-up.
    """
    now = now or now_et()
    if not is_extended_window(now):
        return False
    if calendar_check is not None:
        try:
            return bool(calendar_check(now.date()))
        except Exception:
            log.warning("calendar_check failed; falling back to window-only check",
                        exc_info=True)
    return True


def session_date(now: datetime | None = None) -> date:
    """
    The trading day a moment belongs to.

    Anything from Sunday 20:00 ET onward belongs to Monday's session; likewise
    each weekday evening after 20:00 ET rolls into the next weekday. This keeps
    a routine that fires Sunday night from being credited to Sunday.
    """
    now = now or now_et()
    d = now.date()
    if now.timetz().replace(tzinfo=None) >= EXT_WEEK_OPEN_TIME:
        d = d + timedelta(days=1)
    while d.weekday() > 4:          # roll Sat/Sun forward to Monday
        d = d + timedelta(days=1)
    return d


def order_type_for(now: datetime | None = None) -> str:
    """
    'market' inside regular hours, 'limit' outside.

    Extended-hours books are thin and spreads are wide. A market order placed
    outside 09:30-16:00 ET can fill far from the quote. On a small account a
    single bad fill is a material percentage of equity. Callers MUST respect
    this and supply a limit price when this returns 'limit'.
    """
    return "market" if is_regular_hours(now) else "limit"


# --------------------------------------------------------------------------
# Run-once-per-session ledger
# --------------------------------------------------------------------------

def _state_path() -> Path:
    # STATE_DIR is already mounted to the Railway volume at /data.
    return Path(os.getenv("STATE_DIR", "state")) / "routine_runs.json"


def _load() -> dict[str, str]:
    p = _state_path()
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        log.warning("routine_runs.json unreadable; starting empty", exc_info=True)
        return {}


def _save(data: dict[str, str]) -> None:
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(p)                     # atomic; survives mid-write restart
    except Exception:
        log.error("failed to persist routine_runs.json", exc_info=True)


def has_run(routine: str, now: datetime | None = None) -> bool:
    return _load().get(routine) == session_date(now).isoformat()


def mark_ran(routine: str, now: datetime | None = None) -> None:
    """Call this AFTER a routine completes, not before."""
    data = _load()
    data[routine] = session_date(now).isoformat()
    _save(data)


# --------------------------------------------------------------------------
# The replacement decision
# --------------------------------------------------------------------------

@dataclass
class CatchupDecision:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


def should_run_catchup(
    routine: str,
    now: datetime | None = None,
    calendar_check=None,
) -> CatchupDecision:
    """
    Drop-in replacement for the 2.0h stale cap.

    Allows a late run when all of:
      - the routine is catch-up eligible
      - we are inside the 24/5 window and the market is not closed for a holiday
      - we are before the routine's catch-up deadline on this session date
      - the routine has not already run for this session date
    """
    now = now or now_et()

    deadline = CATCHUP_DEADLINES.get(routine)
    if deadline is None:
        return CatchupDecision(False, f"{routine} is not catch-up eligible")

    if not is_tradeable(now, calendar_check=calendar_check):
        return CatchupDecision(False, "outside 24/5 window or market closed")

    if now.timetz().replace(tzinfo=None) >= deadline:
        return CatchupDecision(
            False, f"past catch-up deadline {deadline.isoformat()} ET"
        )

    if has_run(routine, now):
        return CatchupDecision(
            False, f"already ran for session {session_date(now).isoformat()}"
        )

    return CatchupDecision(
        True,
        f"catch-up allowed for session {session_date(now).isoformat()} "
        f"at {now:%H:%M} ET (orders: {order_type_for(now)})",
    )
