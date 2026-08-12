"""Shared SPY regime-gate dispatch for backtest_harness engines.

Lets engine.run_simulation() and earnings_engine.run_earnings_simulation()
select between the live default gate (regime_gate.py, SMA/ADX) and the
opt-in HMM gate (regime_gate_hmm.py) via the same `regime_gate_mode` string
used live (routines/market_open.py's REGIME_GATE_MODE env var) -
"sma_adx" (default, unchanged behavior) or "hmm".

Exists so both engines can backtest the HMM gate's historical hit rate
before it's ever trusted to gate real trades - see regime_gate_hmm.py's
own module docstring for the "shadow first" rationale.
"""


def classify_spy_regime(spy_bars: list[dict], mode: str = "sma_adx"):
    """`spy_bars` is a list of bar dicts (oldest -> newest) with at least
    high/low/close keys; volume is used opportunistically when mode="hmm"
    and every bar has it."""
    highs = [b["high"] for b in spy_bars]
    lows = [b["low"] for b in spy_bars]
    closes = [b["close"] for b in spy_bars]

    if mode == "hmm":
        from regime_gate_hmm import classify as _classify

        volumes = [b["volume"] for b in spy_bars] if all("volume" in b for b in spy_bars) else None
        return _classify(highs, lows, closes, volumes=volumes)

    from regime_gate import classify as _classify

    return _classify(highs, lows, closes)
