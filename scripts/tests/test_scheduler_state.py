"""Tests for scheduler catch-up state persistence and the 24/5 window logic.

The fixed 2.0h stale cap was retired in favour of core.trading_window: a missed
routine may now catch up anywhere inside Alpaca's 24/5 window, before the
routine's deadline, once per session, and not on a market holiday. These tests
pin the clock and mock the broker so they stay offline and deterministic.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core.broker as broker_mod
import scheduler
from core import config
from core import trading_window


def _fake_broker_cls(*, open_on: bool = True):
    """Broker stand-in.

    is_market_open_on() answers trading_window's holiday calendar check without
    a network call. Touching .trade raises, so the Alpaca order-history dedup
    (_market_open_ran_today) fails safe to 'not run' — which keeps market_open
    eligible so the tests actually exercise should_run_catchup.
    """

    class _FakeBroker:
        def __init__(self, *a, **k):
            pass

        def is_market_open_on(self, day):
            return open_on

        @property
        def trade(self):
            raise RuntimeError("no network in tests")

    return _FakeBroker


def _tuesday(hour: int, minute: int) -> datetime.datetime:
    # Tuesday 2026-07-07. market_open window ends 09:44 ET, midday ends 12:09 ET.
    return scheduler.ET.localize(datetime.datetime(2026, 7, 7, hour, minute))


@pytest.fixture
def sched_env(monkeypatch, tmp_path):
    """Isolate both ledgers to tmp_path and mock the broker (market open, not a
    holiday). The file ledger is scheduler.CATCHUP_FILE; trading_window's
    per-session ledger reads STATE_DIR from the environment."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        scheduler, "CATCHUP_FILE", str(tmp_path / ".scheduler_ran_today.json")
    )
    monkeypatch.setattr(broker_mod, "BrokerClient", _fake_broker_cls(open_on=True))
    return tmp_path


class TestStateDir:
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


class TestCatchup245:
    def test_stale_market_open_now_catches_up(self, sched_env):
        """The 07-22 bug: at 13:15 ET market_open is 3.5h stale. Under the old
        2.0h cap it was skipped and the day was dead. It must now catch up
        (13:15 is inside the 24/5 window and before the 18:00 deadline)."""
        assert scheduler.get_catchup_routine(_tuesday(13, 15)) == "routines.market_open"

    def test_once_per_session_via_file_ledger(self, sched_env):
        now = _tuesday(13, 15)
        assert scheduler.get_catchup_routine(now) == "routines.market_open"
        scheduler._mark_ran(now, "routines.market_open")
        # market_open deduped → the next stale eligible routine is midday_review
        assert scheduler.get_catchup_routine(now) == "routines.midday_review"
        scheduler._mark_ran(now, "routines.midday_review")
        assert scheduler.get_catchup_routine(now) is None

    def test_double_fire_guarded_by_trading_window_ledger(self, sched_env):
        """Even if the file ledger is lost on a Railway redeploy, trading_window's
        per-session ledger stops a second fire of the same routine."""
        now = _tuesday(13, 15)
        trading_window.mark_ran("routines.market_open", now)
        # File ledger is empty, but should_run_catchup sees 'already ran' and
        # falls through to the next eligible routine.
        assert scheduler.get_catchup_routine(now) == "routines.midday_review"

    def test_past_deadline_not_eligible(self, sched_env):
        # 18:30 ET is inside the 24/5 window but past the 18:00 catch-up deadline
        # for both buy routines → nothing to catch up.
        assert scheduler.get_catchup_routine(_tuesday(18, 30)) is None

    def test_market_holiday_blocks_catchup(self, monkeypatch, sched_env):
        # calendar_check reports the market closed that day → no catch-up, even
        # though the clock is inside the 24/5 window and before the deadline.
        monkeypatch.setattr(
            broker_mod, "BrokerClient", _fake_broker_cls(open_on=False)
        )
        assert scheduler.get_catchup_routine(_tuesday(13, 15)) is None

    def test_resets_next_day(self, sched_env):
        now = _tuesday(13, 15)
        scheduler._mark_ran(now, "routines.market_open")
        scheduler._mark_ran(now, "routines.midday_review")
        assert scheduler.get_catchup_routine(now) is None
        # New session: the file ledger is date-keyed, so the next weekday is
        # fresh and market_open is eligible to catch up again.
        next_day = now + datetime.timedelta(days=1)  # Wednesday
        assert scheduler.get_catchup_routine(next_day) == "routines.market_open"
