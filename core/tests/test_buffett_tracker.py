"""Unit tests for core/buffett_tracker.py.

State is redirected to a temp file (monkeypatch BUFFETT_FILE) so tests
never touch the real state/buffett_positions.json.
"""
from __future__ import annotations

import json
import os

import pytest

from core import buffett_tracker


@pytest.fixture(autouse=True)
def _isolated_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(buffett_tracker, "BUFFETT_FILE", os.path.join(str(tmp_path), "buffett_positions.json"))
    yield


def make_snapshot(symbol="AAPL", conviction_score=0.8):
    return {
        "symbol": symbol,
        "conviction_score": conviction_score,
        "fundamentals": {"business_quality": {"score": 0.9}},
        "valuation": {"valuation_details": {"graham_number": 220.0}},
    }


def test_add_and_get_by_symbol_roundtrips_entry_snapshot():
    snapshot = make_snapshot()
    buffett_tracker.add_position("AAPL", entry_price=150.0, qty=10, entry_snapshot=snapshot)

    stored = buffett_tracker.get_by_symbol("AAPL")

    assert stored is not None
    assert stored["entry_price"] == 150.0
    assert stored["qty"] == 10
    assert stored["entry_snapshot"] == snapshot
    assert stored["strategy"] == "buffett_value"
    assert "entry_date" in stored


def test_get_by_symbol_returns_none_when_untracked():
    assert buffett_tracker.get_by_symbol("MSFT") is None


def test_is_tracked():
    assert buffett_tracker.is_tracked("AAPL") is False
    buffett_tracker.add_position("AAPL", entry_price=150.0, qty=10, entry_snapshot=make_snapshot())
    assert buffett_tracker.is_tracked("AAPL") is True


def test_remove_position():
    buffett_tracker.add_position("AAPL", entry_price=150.0, qty=10, entry_snapshot=make_snapshot())
    buffett_tracker.remove_position("AAPL")

    assert buffett_tracker.get_by_symbol("AAPL") is None


def test_remove_position_untracked_symbol_is_a_noop():
    buffett_tracker.remove_position("AAPL")  # must not raise
    assert buffett_tracker.get_all() == {}


def test_get_all_returns_every_tracked_position():
    buffett_tracker.add_position("AAPL", entry_price=150.0, qty=10, entry_snapshot=make_snapshot("AAPL"))
    buffett_tracker.add_position("MSFT", entry_price=300.0, qty=5, entry_snapshot=make_snapshot("MSFT"))

    data = buffett_tracker.get_all()

    assert set(data.keys()) == {"AAPL", "MSFT"}


def test_state_persisted_has_no_hold_days_or_expiry_concept():
    """Regression: buffett_value must never carry a calendar-exit field."""
    buffett_tracker.add_position("AAPL", entry_price=150.0, qty=10, entry_snapshot=make_snapshot())

    stored = buffett_tracker.get_by_symbol("AAPL")

    for key in stored:
        assert "hold_day" not in key.lower()
        assert "expir" not in key.lower()
    assert not hasattr(buffett_tracker, "get_expired")


def test_state_file_written_as_valid_json_via_atomic_replace():
    buffett_tracker.add_position("AAPL", entry_price=150.0, qty=10, entry_snapshot=make_snapshot())

    with open(buffett_tracker.BUFFETT_FILE) as f:
        data = json.load(f)

    assert "AAPL" in data
