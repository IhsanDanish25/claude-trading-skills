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
        scheduled = now.replace(hour=sched_h, minute=m_max, second=0, microsecond=0)
        age_hours = (now - scheduled).total_seconds() / 3600.0
        past_window = age_hours >= 0
        if past_window and age_hours <= CATCHUP_MAX_AGE_HOURS:
            return module
        if past_window and age_hours > CATCHUP_MAX_AGE_HOURS:
            log.info(f"Skipping stale catch-up for {module} "
                     f"({age_hours:.1f}h late, cap={CATCHUP_MAX_AGE_HOURS}h)")

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


def main():
    now = datetime.datetime.now(ET)
    log.info(f"Scheduler fired: {now.strftime('%A %Y-%m-%d %H:%M %Z')}")

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
