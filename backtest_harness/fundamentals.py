"""Historical earnings fetcher for point-in-time backtesting (earnmom).

Uses FMP /stable/earnings?symbol=X (no `limit` param — the free tier returns the
full quarterly history that way; passing limit>=8 triggers a 402). For each
reported quarter we keep epsActual / epsEstimated and precompute a surprise %,
then cache per-symbol so re-runs are offline.

The engine (engine5) serves these rows through its patched FMP _get for the
/earning_calendar endpoint the earnmom screener calls, filtered to <= AS_OF so
there is no look-ahead.

IMPORTANT: fetch BEFORE engine5 is imported (engine5 monkeypatches core.fmp._get
at import time). Callers pass the real getter, or rely on the default which binds
core.fmp._get at call time.
"""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("backtest.fundamentals")

_HERE = os.path.dirname(os.path.abspath(__file__))
EARNINGS_CACHE_DIR = os.path.join(os.path.dirname(_HERE), "cache", "earnings")


def _cache_path(symbol: str) -> str:
    return os.path.join(EARNINGS_CACHE_DIR, f"{symbol.upper()}.json")


def _load_cached(symbol: str) -> list[dict] | None:
    p = _cache_path(symbol)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f).get("earnings", [])
    except (ValueError, OSError):
        return None


def _surprise_pct(actual: float, estimate: float) -> float:
    """Percent surprise vs. estimate. Guard divide-by-zero on a 0 estimate."""
    if estimate is None or actual is None:
        return 0.0
    denom = abs(estimate)
    if denom < 1e-9:
        # No meaningful estimate — treat any positive actual as a modest beat,
        # negative as a miss, so it doesn't silently pass the surprise gate.
        return 0.0
    return (actual - estimate) / denom * 100.0


def _normalize(rows: list) -> list[dict]:
    """Keep only reported quarters (epsActual present), compute surprise, sort
    oldest-first. Shaped so engine5 can re-emit as /earning_calendar rows."""
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        date = r.get("date")
        actual = r.get("epsActual")
        if not date or actual is None:
            continue
        try:
            actual = float(actual)
        except (TypeError, ValueError):
            continue
        est_raw = r.get("epsEstimated")
        try:
            estimate = float(est_raw) if est_raw is not None else None
        except (TypeError, ValueError):
            estimate = None
        out.append({
            "date":         date[:10],
            "epsActual":    actual,
            "epsEstimated": estimate,
            "surprise_pct": round(_surprise_pct(actual, estimate), 4),
        })
    out.sort(key=lambda x: x["date"])
    return out


def fetch_and_cache_earnings(
    symbols: list[str],
    getter=None,
    force: bool = False,
    sleep: float = 0.2,
) -> dict[str, list[dict]]:
    """Return {symbol: [reported-quarter dicts oldest-first]}.

    getter: the FMP _get callable (defaults to core.fmp._get bound at call time —
    call this BEFORE engine5 patches it, or pass the original explicitly).
    """
    os.makedirs(EARNINGS_CACHE_DIR, exist_ok=True)
    if getter is None:
        from core.fmp import _get as getter  # bind now (must be pre-patch)
    from core.fmp import _STABLE as stable

    out: dict[str, list[dict]] = {}
    todo: list[str] = []
    for s in symbols:
        cached = _load_cached(s)
        if cached is not None and not force:
            out[s] = cached
        else:
            todo.append(s)

    log.info("earnings-cache: %d symbols cached, fetching %d from FMP /stable/earnings",
             len(out), len(todo))
    n_ok, n_fail = 0, 0
    for i, sym in enumerate(todo):
        try:
            # NO limit param — full history on the free tier.
            raw = getter(f"{stable}/earnings", {"symbol": sym})
            rows = _normalize(raw if isinstance(raw, list) else [])
            # Only cache a genuine (non-empty) result. A rate-limit/402 raises
            # before this; an empty list here means "no reported earnings" which
            # we DON'T persist either — so a later resumed run retries the symbol
            # rather than treating a transient failure as permanent emptiness.
            if rows:
                with open(_cache_path(sym), "w") as f:
                    json.dump({"symbol": sym.upper(), "earnings": rows}, f)
                n_ok += 1
            out[sym] = rows
        except Exception as e:  # noqa: BLE001
            log.warning("earnings-cache: %s failed (%s)", sym, str(e)[:120])
            out[sym] = []
            n_fail += 1
        if sleep and i < len(todo) - 1:
            time.sleep(sleep)
        if (i + 1) % 40 == 0:
            log.info("earnings-cache: fetched %d/%d symbols", i + 1, len(todo))

    total_q = sum(len(v) for v in out.values())
    log.info("earnings-cache: %d symbols total | %d newly fetched, %d failed | "
             "%d quarters", len(out), n_ok, n_fail, total_q)
    if n_fail:
        log.warning("earnings-cache: %d symbols missing (likely FMP daily quota) "
                    "— re-run after quota reset to complete the universe", n_fail)
    return out
