"""Tests for scheduler catch-up state persistence."""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scheduler
from core import config


def _tuesday_1315():
    # Tuesday 2026-07-07 13:15 ET — past midday window (12:09), within 2h cap;
    # market_open (09:44) is 3.5h stale.
    return scheduler.ET.localize(datetime.datetime(2026, 7, 7, 13, 15))


class TestStatePersistence:
    def test_state_dir_honors_env_var(self, monkeypatch, tmp_path):
        """Regression: scheduler hardcoded its own repo-relative STATE_DIR
        instead of the env-aware core.config.STATE_DIR, so ran-today state
        never landed on the persistent volume and every container restart
        re-ran 'missed' routines."""
        import importlib

        monkeypatch.setenv("STATE_DIR", str(tmp_path))
        try:
            importlib.reload(config)
            importlib.reload(scheduler)
            assert scheduler.STATE_DIR == str(tmp_path)
            assert scheduler.CATCHUP_FILE.startswith(str(tmp_path))
        finally:
            monkeypatch.delenv("STATE_DIR")
            importlib.reload(config)
            importlib.reload(scheduler)

    def test_catchup_skipped_after_mark_ran(self, monkeypatch, tmp_path):
        monkeypatch.setattr(scheduler, "STATE_DIR", str(tmp_path))
        monkeypatch.setattr(scheduler, "CATCHUP_FILE",
                            str(tmp_path / ".scheduler_ran_today.json"))
        now = _tuesday_1315()

        assert scheduler.get_catchup_routine(now) == "routines.midday_review"
        scheduler._mark_ran(now, "routines.midday_review")
        # Fresh read of the state file — same day, already ran → no catch-up
        assert scheduler.get_catchup_routine(now) is None

    def test_catchup_resets_next_day(self, monkeypatch, tmp_path):
        monkeypatch.setattr(scheduler, "STATE_DIR", str(tmp_path))
        monkeypatch.setattr(scheduler, "CATCHUP_FILE",
                            str(tmp_path / ".scheduler_ran_today.json"))
        now = _tuesday_1315()
        scheduler._mark_ran(now, "routines.midday_review")

        next_day = now + datetime.timedelta(days=1)
        assert scheduler.get_catchup_routine(next_day) == "routines.midday_review"
