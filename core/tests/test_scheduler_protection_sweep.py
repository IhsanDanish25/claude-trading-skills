"""Unit tests for scheduler.run_protection_sweep.

Regression coverage for the 2026-08-13 MSFT incident: a fractional
position's DAY-tif stop expired at market_close's cancel-all and stayed
naked until market_open's repair pass the next morning (~17.5 hours).
worker.py fires scheduler.py every 10 minutes 24/7, so running the same
idempotent repair (core.protection.reattach_missing_protection) on every
tick — not just at the three daily checkpoints — bounds that gap to
~10 minutes instead.

Pure-logic tests: BrokerClient and reattach_missing_protection are mocked,
no live Alpaca network calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import scheduler


def test_sweep_calls_reattach_missing_protection(monkeypatch):
    broker_instance = MagicMock()
    monkeypatch.setattr("core.broker.BrokerClient", lambda: broker_instance)

    reattach = MagicMock(return_value=set())
    monkeypatch.setattr("core.protection.reattach_missing_protection", reattach)

    scheduler.run_protection_sweep()

    reattach.assert_called_once()
    args = reattach.call_args[0]
    assert args[0] is broker_instance


def test_sweep_logs_but_does_not_raise_when_flatten_happens(monkeypatch):
    monkeypatch.setattr("core.broker.BrokerClient", lambda: MagicMock())
    monkeypatch.setattr(
        "core.protection.reattach_missing_protection", lambda *a, **k: {"MSFT"}
    )

    # Must not raise even though a position was flattened.
    scheduler.run_protection_sweep()


def test_sweep_swallows_exceptions_so_the_tick_still_dispatches(monkeypatch):
    def _boom():
        raise RuntimeError("Alpaca down")

    monkeypatch.setattr("core.broker.BrokerClient", _boom)

    # Must not raise — a broker/network failure here shouldn't block the
    # rest of the tick (routine dispatch, catch-up logic, etc.).
    scheduler.run_protection_sweep()
