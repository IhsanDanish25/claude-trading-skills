"""Unit tests for analyst.py's screen_for_buffett_candidates -- the
thread-pool concurrent screen added to unblock market_open (a ~100-symbol
serial yfinance screen took several minutes; see routines/market_open.py's
_run_buffett_value). analyze_symbol itself is mocked so these are pure
concurrency/aggregation tests, no network calls.
"""

import analyst


def make_candidate(symbol, conviction_score, passes=True):
    return {"symbol": symbol, "conviction_score": conviction_score, "passes_screen": passes}


def test_all_symbols_get_processed_under_concurrency(monkeypatch):
    symbols = [f"SYM{i}" for i in range(25)]  # more than SCREEN_MAX_WORKERS

    def _fake_analyze(symbol, lookback_years):
        return make_candidate(symbol, conviction_score=0.7)

    monkeypatch.setattr(analyst, "analyze_symbol", _fake_analyze)

    candidates = analyst.screen_for_buffett_candidates(symbols)

    assert {c["symbol"] for c in candidates} == set(symbols)


def test_failing_symbols_do_not_lose_others(monkeypatch):
    def _fake_analyze(symbol, lookback_years):
        if symbol == "BOOM":
            raise RuntimeError("yfinance blew up")
        return make_candidate(symbol, conviction_score=0.7)

    monkeypatch.setattr(analyst, "analyze_symbol", _fake_analyze)

    candidates = analyst.screen_for_buffett_candidates(["AAPL", "BOOM", "MSFT"])

    assert {c["symbol"] for c in candidates} == {"AAPL", "MSFT"}


def test_only_passing_candidates_are_returned(monkeypatch):
    def _fake_analyze(symbol, lookback_years):
        return make_candidate(symbol, conviction_score=0.5, passes=(symbol == "AAPL"))

    monkeypatch.setattr(analyst, "analyze_symbol", _fake_analyze)

    candidates = analyst.screen_for_buffett_candidates(["AAPL", "MSFT"])

    assert [c["symbol"] for c in candidates] == ["AAPL"]


def test_results_sorted_by_conviction_score_regardless_of_completion_order(monkeypatch):
    import random
    import time as _time

    scores = {"LOW": 0.3, "HIGH": 0.9, "MID": 0.6}

    def _fake_analyze(symbol, lookback_years):
        # Randomized tiny delay so completion order can't match input order.
        _time.sleep(random.uniform(0, 0.01))
        return make_candidate(symbol, conviction_score=scores[symbol])

    monkeypatch.setattr(analyst, "analyze_symbol", _fake_analyze)

    candidates = analyst.screen_for_buffett_candidates(["LOW", "HIGH", "MID"])

    assert [c["symbol"] for c in candidates] == ["HIGH", "MID", "LOW"]


def test_none_result_from_analyze_symbol_is_skipped(monkeypatch):
    def _fake_analyze(symbol, lookback_years):
        return None if symbol == "NODATA" else make_candidate(symbol, 0.8)

    monkeypatch.setattr(analyst, "analyze_symbol", _fake_analyze)

    candidates = analyst.screen_for_buffett_candidates(["AAPL", "NODATA"])

    assert [c["symbol"] for c in candidates] == ["AAPL"]


def test_respects_custom_max_workers(monkeypatch):
    seen_concurrent = {"count": 0, "max": 0}
    import threading

    lock = threading.Lock()

    def _fake_analyze(symbol, lookback_years):
        with lock:
            seen_concurrent["count"] += 1
            seen_concurrent["max"] = max(seen_concurrent["max"], seen_concurrent["count"])
        import time as _t
        _t.sleep(0.02)
        with lock:
            seen_concurrent["count"] -= 1
        return make_candidate(symbol, 0.7)

    monkeypatch.setattr(analyst, "analyze_symbol", _fake_analyze)

    analyst.screen_for_buffett_candidates([f"S{i}" for i in range(10)], max_workers=2)

    assert seen_concurrent["max"] <= 2
