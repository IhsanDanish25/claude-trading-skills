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
    # market_open:  9:30 AM Mon-Fri (window 9:30-9:39)
    (9,  30, 39, 0, 4, "routines.market_open"),
    # midday:      12:00 PM Mon-Fri (window 12:00-12:09)
    (12,  0,  9, 0, 4, "routines.midday_review"),
    # market_close: 3:00 PM Mon-Fri (window 15:00-15:09)
    (15,  0,  9, 0, 4, "routines.market_close"),
    # weekly:       4:00 PM Friday only (window 16:00-16:09)
    (16,  0,  9, 4, 4, "routines.weekly_review"),
]


def get_routine(now: datetime.datetime):
    h, m, wd = now.hour, now.minute, now.weekday()

    for (sched_h, m_min, m_max, wd_min, wd_max, module) in SCHEDULE:
        if h == sched_h and m_min <= m <= m_max and wd_min <= wd <= wd_max:
            return module
    return None


def run_routine(module: str):
    import importlib
    log.info(f"Importing {module}")
    mod = importlib.import_module(module)
    mod.run()


def main():
    now = datetime.datetime.now(ET)
    log.info(f"Scheduler fired: {now.strftime('%A %Y-%m-%d %H:%M %Z')}")

    routine = get_routine(now)

    if routine is None:
        log.info(f"No routine scheduled for {now.strftime('%H:%M')} — exiting")
        return

    log.info(f"Dispatching → {routine}")
    try:
        run_routine(routine)
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
