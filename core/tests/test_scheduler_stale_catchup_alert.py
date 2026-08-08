"""Unit tests for scheduler.py's stale-catchup give-up path.

Regression coverage for the 2026-08-06 incident: market_open timed out on
every catch-up attempt, and once the 2h catch-up cap was hit the scheduler
silently gave up — visible only by reading Railway logs line-by-line or
waiting for the EOD summary hours later. `_record_skipped_routine` now
reports (via return value) the first time a module is given up on for the
day, so the caller can fire an immediate alert instead.

Pure-logic tests: no live Alpaca/EDGAR/notifier network calls.
"""

from __future__ import annotations

import datetime

import scheduler


def test_record_skipped_routine_true_only_on_first_call_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler, "SKIPPED_ROUTINES_FILE", str(tmp_path / "skipped_routines.json"))

    now = datetime.datetime(2026, 8, 6, 11, 49, 0)

    first = scheduler._record_skipped_routine(now, "routines.market_open", "2.1h late, cap=2.0h")
    second = scheduler._record_skipped_routine(now, "routines.market_open", "2.3h late, cap=2.0h")

    assert first is True
    assert second is False


def test_record_skipped_routine_true_again_for_a_different_module(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler, "SKIPPED_ROUTINES_FILE", str(tmp_path / "skipped_routines.json"))

    now = datetime.datetime(2026, 8, 6, 12, 20, 0)

    scheduler._record_skipped_routine(now, "routines.market_open", "2.6h late, cap=2.0h")
    is_first_for_midday = scheduler._record_skipped_routine(
        now, "routines.midday_review", "0.5h late, cap=2.0h"
    )

    assert is_first_for_midday is True


def test_get_catchup_routine_alerts_exactly_once_when_giving_up(tmp_path, monkeypatch):
    """Simulate 3 consecutive ticks past the 2h cap for the same module —
    the notifier must fire on the first tick only, not every 10-min retick."""
    monkeypatch.setattr(scheduler, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler, "SKIPPED_ROUTINES_FILE", str(tmp_path / "skipped_routines.json"))
    monkeypatch.setattr(scheduler, "CATCHUP_FILE", str(tmp_path / ".scheduler_ran_today.json"))
    monkeypatch.setattr(scheduler, "_market_open_ran_today", lambda: False)

    alerts = []
    monkeypatch.setattr(
        "core.notifier.send_error_alert",
        lambda routine, error: alerts.append((routine, error)) or True,
    )

    et = scheduler.ET
    tick_times = [
        et.localize(datetime.datetime(2026, 8, 6, 11, 49, 0)),
        et.localize(datetime.datetime(2026, 8, 6, 11, 59, 0)),
        et.localize(datetime.datetime(2026, 8, 6, 12, 9, 0)),
    ]
    for now in tick_times:
        scheduler.get_catchup_routine(now)

    assert len(alerts) == 1
    routine, error = alerts[0]
    assert routine == "routines.market_open"
    assert "Gave up" in error


def test_midday_review_alpaca_fallback_suppresses_stale_alert(tmp_path, monkeypatch):
    """Regression for the 2026-08-07 false-positive: a lost state file (e.g.
    from a Railway redeploy) should not trigger a stale-catchup alert for
    midday_review if Alpaca order history proves it already bought today —
    mirrors the existing market_open fallback."""
    monkeypatch.setattr(scheduler, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler, "SKIPPED_ROUTINES_FILE", str(tmp_path / "skipped_routines.json"))
    monkeypatch.setattr(scheduler, "CATCHUP_FILE", str(tmp_path / ".scheduler_ran_today.json"))
    monkeypatch.setattr(scheduler, "_market_open_ran_today", lambda: True)
    monkeypatch.setattr(scheduler, "_midday_review_ran_today", lambda: True)

    alerts = []
    monkeypatch.setattr(
        "core.notifier.send_error_alert",
        lambda routine, error: alerts.append((routine, error)) or True,
    )

    now = scheduler.ET.localize(datetime.datetime(2026, 8, 7, 19, 30, 0))
    assert scheduler.get_catchup_routine(now) is None
    assert alerts == []
