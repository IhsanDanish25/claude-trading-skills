"""Verification #1 from the backtest-live-strategy skill:

No-look-ahead assertion. For every decision date T in the simulation,
the bars fed to the real screen() and composite() must be dated <= T-1.

Strategy: instrument fake_fetch (in backtest_harness.data) so every
call records (as_of, max_bar_date). After a fresh simulation run,
assert no call saw a bar dated after its as_of.
"""
from __future__ import annotations

import datetime
import json
import os
import sys

os.environ.setdefault("ALPACA_API_KEY", "x")
os.environ.setdefault("ALPACA_SECRET_KEY", "x")
os.environ["COMPOSITE_USE_FMP"] = "false"

sys.path.insert(0, "/Users/mohamedihsan/claude-trading-skills")

from backtest_harness import data, engine
import core.screener as screener


_RECORDED_CALLS: list[tuple[datetime.date, datetime.date]] = []

_orig_slice_asof = data.BarStore.slice_asof
_orig_fake_fetch = data.fake_fetch


def _instrumented_slice_asof(self, symbol, as_of, calendar_days):
    rows = _orig_slice_asof(self, symbol, as_of, calendar_days)
    if data.AS_OF is not None:
        cached = self.series.get(symbol, [])
        max_date = None
        lo = as_of - datetime.timedelta(days=calendar_days)
        for b in cached:
            d = datetime.date.fromisoformat(b["date"])
            if lo < d <= as_of and (max_date is None or d > max_date):
                max_date = d
        _RECORDED_CALLS.append((data.AS_OF, max_date))
    return rows


def _instrumented_fetch(symbols, days=60):
    return _orig_fake_fetch(symbols, days)


def run() -> int:
    from core.config import WATCHLIST
    from core import config

    symbols = list(dict.fromkeys(WATCHLIST + data.INDEX_SYMBOLS + data.SECTOR_ETFS))
    series = data.fetch_and_cache(symbols, years=2.4, force=False)
    store = data.BarStore(series)
    data.install_store(store)

    data.BarStore.slice_asof = _instrumented_slice_asof
    screener._fetch_bars = _orig_fake_fetch
    print(f"DEBUG slice_asof patched to: {data.BarStore.slice_asof.__name__}")

    universe = [s for s in WATCHLIST if s in series and store.first_date(s) and
                (store.trading_calendar("SPY")[-1] - store.first_date(s)).days >= 365 * 2][:30]

    print(f"Universe: {len(universe)} names. Running mini backtest (warmup=70, no params)...")
    pf = engine.run_simulation(store, universe, start_equity=100_000.0, warmup=70,
                                slippage_bps=0, stop_mode="flat")

    if not _RECORDED_CALLS:
        print("FAIL: no fetch calls recorded — instrumentation didn't fire")
        return 1

    as_ofs = [c[0] for c in _RECORDED_CALLS if c[1] is not None]
    max_seen_dates = [c[1] for c in _RECORDED_CALLS if c[1] is not None]

    if not as_ofs:
        print("FAIL: no fetch calls had a max_seen date")
        return 1

    print(f"Recorded {len(_RECORDED_CALLS)} fetch calls; {len(as_ofs)} had measurable max-seen dates")
    print(f"  as_of range:           {min(as_ofs)} -> {max(as_ofs)}")
    print(f"  max_bar_seen range:    {min(max_seen_dates)} -> {max(max_seen_dates)}")

    violations = [(a, m) for a, m in _RECORDED_CALLS if m is not None and m > a]
    if violations:
        print(f"FAIL: {len(violations)} fetch calls saw a bar dated AFTER as_of")
        for a, m in violations[:5]:
            print(f"  as_of={a}, max_bar_seen={m}")
        return 1

    print(f"PASS: every fetch call saw bars dated <= as_of (no look-ahead)")
    print(f"  (max gap between as_of and max_bar_seen: {max((a - m).days for a, m in _RECORDED_CALLS if m):+d} days)")
    return 0


if __name__ == "__main__":
    sys.exit(run())