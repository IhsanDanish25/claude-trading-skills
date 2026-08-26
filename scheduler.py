"""
MASTER SCHEDULER
Railway worker cron: */10 6-16 * * 1-5
Runs every 10min during market hours Mon-Fri.
Checks current time → dispatches correct routine.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import datetime
import pytz
import logging
import traceback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | scheduler | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scheduler")
ET  = pytz.timezone("America/New_York")

# ── Schedule ──────────────────────────────────────────────────────────────────
# Each entry: (hour, minute_min, minute_max, weekday_min, weekday_max, module)
# weekday: 0=Mon 4=Fri
SCHEDULE = [
    # pre_market:   6:00 AM Mon-Fri (window 6:00-6:09)
    (6,   0,  9, 0, 4, "routines.pre_market"),
    # market_open:  9:35 AM Mon-Fri (window 9:35-9:44, aligned with the
    #               ENTRY_DELAY_MIN=5 entry gate so the firing tick always
    #               lands at/after 9:35 and entries are not blocked)
    (9,  35, 44, 0, 4, "routines.market_open"),
    # midday:      12:00 PM Mon-Fri (window 12:00-12:09)
    (12,  0,  9, 0, 4, "routines.midday_review"),
    # market_close: 3:00 PM Mon-Fri (window 15:00-15:09)
    (15,  0,  9, 0, 4, "routines.market_close"),
    # weekly:       4:00 PM Friday only (window 16:00-16:09)
    (16,  0,  9, 4, 4, "routines.weekly_review"),
    # weekly_csp:   9:45 AM Monday-Friday (window 9:45-9:54) — generate CSP picks
    (9,  45, 54, 0, 4, "routines.weekly_csp"),
]


# Must be the env-aware config STATE_DIR: on Railway that points at the
# persistent volume, so ran-today state survives container restarts and
# catch-up doesn't re-fire routines that already ran.
from core.config import STATE_DIR

CATCHUP_FILE = os.path.join(STATE_DIR, ".scheduler_ran_today.json")
CATCHUP_MAX_AGE_HOURS = 2.0
SKIPPED_ROUTINES_FILE = os.path.join(STATE_DIR, "skipped_routines.json")


def _record_skipped_routine(now: datetime.datetime, module: str, reason: str) -> bool:
    """Persist a stale-catchup skip so the EOD summary can surface it — a
    routine silently skipped (e.g. worker down past the 2h catch-up cap)
    would otherwise be visible only in Railway logs, the same blind spot
    that let the 2026-08-05 flatten alerts go unnoticed.

    Returns True the first time `module` is recorded as skipped today, so
    the caller can send a same-day alert exactly once instead of waiting
    for the EOD summary hours later (or spamming one per 10-min tick)."""
    try:
        import json
        os.makedirs(STATE_DIR, exist_ok=True)
        today = now.strftime("%Y-%m-%d")
        data = {"date": today, "skipped": []}
        if os.path.exists(SKIPPED_ROUTINES_FILE):
            with open(SKIPPED_ROUTINES_FILE) as f:
                existing = json.load(f)
            if existing.get("date") == today:
                data = existing
        is_first_today = not any(s["module"] == module for s in data["skipped"])
        # Overwrite rather than append per-module — the stale-catchup check
        # re-fires every 10-min tick for the rest of the day, and duplicate
        # rows for the same routine add noise rather than information.
        data["skipped"] = [s for s in data["skipped"] if s["module"] != module]
        data["skipped"].append({
            "module": module, "reason": reason,
            "at": now.strftime("%H:%M:%S"),
        })
        with open(SKIPPED_ROUTINES_FILE, "w") as f:
            json.dump(data, f)
        return is_first_today
    except Exception:
        return False

# ── Fix 8: Alpaca-backed dedup (resilient to Railway ephemeral filesystem) ───
def _market_open_ran_today() -> bool:
    """
    Double-check: even if the state-file was lost due to a redeploy, we can
    still verify that a routine ran today via Alpaca order history.

    If market_open already filled BUY orders today → market_open already ran.
    If no orders filled today → market_open may not have run; let catchup fire.
    """
    try:
        from core.broker import BrokerClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        import datetime as _dt, pytz as _pytz
        ET = _pytz.timezone("America/New_York")
        today_open = _dt.datetime.now(ET).replace(
            hour=9, minute=30, second=0, microsecond=0
        )
        # Query orders filled today (after market open window)
        broker = BrokerClient()
        orders = broker.trade.get_orders(
            GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=today_open.isoformat(),
                limit=10,
            )
        )
        # Anything filled today is proof that a BUY routine ran
        for o in orders:
            if o.side.value == "buy" and (o.filled_qty or 0) > 0:
                return True
        return False
    except Exception:
        return False  # Fail-safe: if we can't check, let catchup fire


def _midday_review_ran_today() -> bool:
    """
    Same double-check as _market_open_ran_today, scoped to midday_review's
    own window: even if the state-file was lost due to a redeploy, a BUY
    filled at/after 12:00 ET today is proof midday_review already ran.

    Most days midday_review finds no candidates and buys nothing, so this
    is a secondary safety net on top of the persisted state file — a
    no-trade day still relies on that state file to avoid a false-positive
    stale-catchup alert.
    """
    try:
        from core.broker import BrokerClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        import datetime as _dt, pytz as _pytz
        ET = _pytz.timezone("America/New_York")
        midday_start = _dt.datetime.now(ET).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        broker = BrokerClient()
        orders = broker.trade.get_orders(
            GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=midday_start.isoformat(),
                limit=10,
            )
        )
        for o in orders:
            if o.side.value == "buy" and (o.filled_qty or 0) > 0:
                return True
        return False
    except Exception:
        return False  # Fail-safe: if we can't check, let catchup fire


def get_routine(now: datetime.datetime):
    h, m, wd = now.hour, now.minute, now.weekday()

    for (sched_h, m_min, m_max, wd_min, wd_max, module) in SCHEDULE:
        if h == sched_h and m_min <= m <= m_max and wd_min <= wd <= wd_max:
            return module
    return None


def get_catchup_routine(now: datetime.datetime):
    """If we're past a routine's window and it hasn't run today, catch up.
    Only catches up market_open and midday_review (the buy routines).
    Stale cap: skip catch-ups that are more than CATCHUP_MAX_AGE_HOURS late."""
    h, m, wd = now.hour, now.minute, now.weekday()
    if wd > 4:
        return None

    ran_today = _load_ran_today(now)

    catchup_targets = [
        (9, 35, 44, "routines.market_open"),
        (12, 0, 9, "routines.midday_review"),
    ]

    for (sched_h, m_min, m_max, module) in catchup_targets:
        if module in ran_today:
            continue
        # Fix 8: double-check against Alpaca so a lost state-file doesn't
        # cause a false-positive "already ran" claim after a Railway redeploy.
        if module == "routines.market_open" and _market_open_ran_today():
            log.info("market_open: ran today (Alpaca history confirms)")
            continue
        if module == "routines.midday_review" and _midday_review_ran_today():
            log.info("midday_review: ran today (Alpaca history confirms)")
            continue
        scheduled = now.replace(hour=sched_h, minute=m_max, second=0, microsecond=0)
        age_hours = (now - scheduled).total_seconds() / 3600.0
        past_window = age_hours >= 0
        if past_window and age_hours <= CATCHUP_MAX_AGE_HOURS:
            return module
        if past_window and age_hours > CATCHUP_MAX_AGE_HOURS:
            reason = f"{age_hours:.1f}h late, cap={CATCHUP_MAX_AGE_HOURS}h"
            log.info(f"Skipping stale catch-up for {module} ({reason})")
            if _record_skipped_routine(now, module, reason):
                # First time today this module was given up on — alert now
                # instead of only surfacing in the EOD summary hours later.
                try:
                    from core.notifier import send_error_alert
                    send_error_alert(
                        module,
                        f"Gave up on catch-up after repeated timeouts/misses "
                        f"({reason}). No further attempts will be made for "
                        f"this routine today.",
                    )
                except Exception:
                    pass

    return None


def _load_ran_today(now: datetime.datetime) -> set:
    try:
        import json
        os.makedirs(STATE_DIR, exist_ok=True)
        if not os.path.exists(CATCHUP_FILE):
            return set()
        with open(CATCHUP_FILE) as f:
            data = json.load(f)
        if data.get("date") != now.strftime("%Y-%m-%d"):
            return set()
        return set(data.get("ran", []))
    except Exception:
        return set()


def _mark_ran(now: datetime.datetime, module: str):
    try:
        import json
        os.makedirs(STATE_DIR, exist_ok=True)
        ran = _load_ran_today(now)
        ran.add(module)
        with open(CATCHUP_FILE, "w") as f:
            json.dump({"date": now.strftime("%Y-%m-%d"), "ran": sorted(ran)}, f)
    except Exception:
        pass


def run_routine(module: str):
    import importlib
    log.info(f"Importing {module}")
    mod = importlib.import_module(module)
    mod.run()


def run_protection_sweep():
    """Verify every open position still has a live stop, every tick — not
    just at the three daily checkpoints (market_open/midday/market_close).

    A fractional position's stop can only be a DAY-tif order (Alpaca
    rejects GTC on fractional qty), so it expires at that session's close
    and the position is naked until the next checkpoint re-attaches one.
    Previously that gap ran from market_close's cancel-all (~15:07 ET)
    until market_open's repair pass (~09:35 ET) — MSFT sat unprotected for
    over 17 hours on 2026-08-13 as a result. worker.py fires this module
    every 10 minutes 24/7 (get_routine's market-hours window only gates
    the scheduled routines, not the process itself), so running the sweep
    unconditionally on every tick bounds the gap to ~10 minutes instead,
    including overnight — Alpaca accepts a DAY order submitted while the
    market is closed and simply queues it for the next session.

    reattach_missing_protection is idempotent (skips symbols that already
    have a live stop), so calling it here as well as from the checkpoint
    routines in the same tick is harmless.
    """
    try:
        from core import config as core_config
        from core.broker import BrokerClient
        from core.protection import reattach_missing_protection

        broker = BrokerClient()
        flattened = reattach_missing_protection(broker, core_config, log)
        if flattened:
            log.warning(f"Protection sweep flattened (attach failed): {sorted(flattened)}")
    except Exception:
        log.error("Protection sweep failed", exc_info=True)


def main():
    now = datetime.datetime.now(ET)
    log.info(f"Scheduler fired: {now.strftime('%A %Y-%m-%d %H:%M %Z')}")

    run_protection_sweep()

    routine = get_routine(now)

    # Catch-up: if we missed a window (e.g. redeploy), run it now
    if routine is None:
        catchup = get_catchup_routine(now)
        if catchup:
            log.info(f"CATCH-UP: {catchup} was missed — running now")
            routine = catchup

    if routine is None:
        log.debug(f"No routine scheduled for {now.strftime('%H:%M')} — exiting")
    else:
        log.info(f"Dispatching → {routine}")
        try:
            run_routine(routine)
            _mark_ran(now, routine)
            log.info(f"Routine complete: {routine}")
        except Exception as e:
            log.error(f"Routine FAILED: {routine} | {e}", exc_info=True)
            try:
                from core.notifier import send_error_alert
                send_error_alert(routine, traceback.format_exc())
            except Exception:
                pass
            sys.exit(1)


if __name__ == "__main__":
    main()
