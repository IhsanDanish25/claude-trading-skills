"""Unit tests for the dynamic Alpaca universe (core/universe.py).

Pure-logic + graceful-fallback tests — no Alpaca / network. With alpaca absent
(or the screener failing), build_universe must fall back to the static watchlist
and never raise or return empty.
"""
from core import universe as u
from core.config import WATCHLIST


def test_clean_seed_first_and_dedup():
    out = u._clean_universe(["NVDA", "aapl", "tsla"], seed=["AAPL", "MSFT"], cap=50)
    assert out[0] == "AAPL" and out[1] == "MSFT"      # seed kept first
    assert out.count("AAPL") == 1                     # case-insensitive dedup
    assert "NVDA" in out and "TSLA" in out


def test_clean_drops_leveraged_etps():
    out = u._clean_universe(["TQQQ", "SQQQ", "SOXL", "UVXY", "TSLL", "NVDA"], seed=[])
    assert out == ["NVDA"]


def test_clean_drops_malformed_tickers():
    out = u._clean_universe(["FOO.W", "ABCDEF", "BRK.B", "GOOGL", "X"], seed=[])
    # warrants/units suffixes and >5-char tickers rejected; plain tickers kept
    assert "GOOGL" in out and "X" in out
    assert "FOO.W" not in out and "ABCDEF" not in out


def test_clean_respects_cap():
    out = u._clean_universe([f"AB{c}" for c in "ABCDEFGHIJ"], seed=["AAA"], cap=4)
    assert len(out) == 4 and out[0] == "AAA"


def test_clean_empty_raw_returns_seed():
    assert u._clean_universe([], seed=["AAA", "BBB"]) == ["AAA", "BBB"]


def test_build_universe_never_empty_without_alpaca():
    # alpaca not installed in test env → lazy import fails → watchlist fallback
    out = u.build_universe()
    assert out, "universe must never be empty"
    assert set(out) <= set(WATCHLIST) or len(out) >= len(WATCHLIST)


def test_build_universe_disabled_returns_watchlist(monkeypatch):
    monkeypatch.setattr(u, "USE_DYNAMIC_UNIVERSE", False)
    assert u.build_universe() == list(WATCHLIST)


def test_build_universe_merges_dynamic(monkeypatch):
    monkeypatch.setattr(u, "USE_DYNAMIC_UNIVERSE", True)
    monkeypatch.setattr(u, "_fetch_alpaca_movers", lambda: ["PLTR", "SOFI", "TQQQ", "AAPL"])
    out = u.build_universe()
    assert "PLTR" in out and "SOFI" in out      # new dynamic names included
    assert "TQQQ" not in out                     # leveraged ETP filtered
    assert "AAPL" in out                         # watchlist seed preserved
    assert out.count("AAPL") == 1                # dedup vs seed
