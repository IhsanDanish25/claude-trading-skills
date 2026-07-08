"""
Insider screener — FMP /stable/insider-trading, transaction type: P-Purchase.

P-Purchase = "Purchase" transactions filed on Form 4 — the clearest insider signal:
  - Directors buying their own stock (no banker's pitch, pure conviction)
  - Scored on: CEO/CFO seniority, cluster count (multiple buys near same price),
    dollar value relative to market cap, and recency weighting.

FMP drain fix: one batch call + in-process cache (same Python process).
1 FMP call per screener invocation vs 103 before.

Pipeline:
  1. In-process cache (same run, survives across screen() calls)
  2. Fetch insider transactions via single FMP /stable/insider-trading call
  3. Filter to transactionType == "P-Purchase" in last N days
  4. Score each transaction; aggregate per symbol
  5. Return sorted by aggregate insider_score

FMP /stable/ returns: [{symbol, transactionDate, transactionType,
   securitiesOwned, sharesAmount, price, finalAmount, isDirector, name}, ...]
"""
from __future__ import annotations

import datetime
import logging
from collections import defaultdict

from core.config import (
    INSIDER_HOLD_DAYS, INSIDER_STOP_PCT, INSIDER_SIZE_PCT,
    INSIDER_MIN_PRICE, INSIDER_MIN_DOLLAR, INSIDER_LOOKBACK_DAYS,
    INSIDER_LIMIT, SP80_UNIVERSE,
)
from core.fmp import _get, _STABLE as _stable, fmp_remaining_calls

log = logging.getLogger(__name__)

# In-process cache — survives across screen() calls within the same run.
_all_transactions_cache: list[dict] | None = None

# Seniority multipliers: CEO=most signal, CFO=strong, VP/Director=moderate
_SENIORITY_WEIGHT = {
    "CEO":       40,
    "CFO":       30,
    "COO":       25,
    "President": 25,
    "Chairman":  30,
    "Director":  10,
    "VP":        8,
    "Other":     5,
}


def _parse_name(title: str) -> str:
    """Extract clean title from full name string like 'JOHN DOE, CEO'."""
    if not title:
        return "Other"
    t = title.upper()
    for key in _SENIORITY_WEIGHT:
        if key in t:
            return key
    return "Other"


def _cluster_score(transactions: list[dict]) -> int:
    """+3 per additional same-week transaction (cluster = conviction signal)."""
    if len(transactions) <= 1:
        return 0
    return min(30, (len(transactions) - 1) * 3)


def _dollars_score(final_amount: float) -> int:
    if final_amount >= 5_000_000:
        return 20
    if final_amount >= 500_000:
        return 10
    return 0


def _recency_multiplier(transaction_date: str, today: datetime.date) -> float:
    """Linear discount: 1.0 for today, 0.0 for older than LOOKBACK_DAYS."""
    try:
        d = datetime.date.fromisoformat(transaction_date[:10])
    except (ValueError, TypeError):
        return 0.0
    age = (today - d).days
    if age < 0:
        return 1.0
    if age >= INSIDER_LOOKBACK_DAYS:
        return 0.0
    return 1.0 - (age / INSIDER_LOOKBACK_DAYS)


def _seniority_score(is_director: bool, name: str, title: str) -> int:
    """Raw seniority score without recency."""
    t = (title or name or "").upper()
    for key, pts in _SENIORITY_WEIGHT.items():
        if key in t:
            return pts
    return _SENIORITY_WEIGHT["Other"]


def screen() -> list[dict]:
    """
    Run insider purchase screen. Returns top-LIMIT candidates sorted by
    aggregate insider_score.
    """
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=INSIDER_LOOKBACK_DAYS)

    # ── Single FMP call + in-process cache ─────────────────────────────────────
    global _all_transactions_cache
    if _all_transactions_cache is not None:
        all_transactions = _all_transactions_cache
        log.info(f"  Using in-process cache ({len(all_transactions)} transactions)")
    elif fmp_remaining_calls() < 1:
        log.warning(
            "Insider SKIPPED: FMP budget exhausted (%d remaining). "
            "Add FMP Starter tier for full signals.",
            fmp_remaining_calls()
        )
        return []
    else:
        today_s  = today.isoformat()
        cutoff_s = cutoff.isoformat()
        log.info(f"Insider: fetching via FMP batch (1 call, {INSIDER_LOOKBACK_DAYS}d lookback)")
        try:
            data = _get(f"{_stable}/insider-trading", {
                "from":  cutoff_s,
                "to":    today_s,
            })
            all_transactions = data if isinstance(data, list) else []
        except Exception as e:
            log.warning(f"FMP batch failed: {e} — insider screen unavailable")
            all_transactions = []
        if all_transactions:
            _all_transactions_cache = all_transactions
        log.info(f"  Batch returned {len(all_transactions)} total transactions")

    log.info(f"  Total transactions available: {len(all_transactions)}")

    purchases = [
        t for t in all_transactions
        if isinstance(t, dict) and t.get("transactionType") == "P-Purchase"
    ]
    log.info(f"  P-Purchases in window ({INSIDER_LOOKBACK_DAYS}d): {len(purchases)}")

    # Aggregate by symbol
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for t in purchases:
        sym = t.get("symbol")
        if not sym:
            continue
        by_symbol[sym].append(t)

    candidates: list[dict] = []
    for sym, txns in by_symbol.items():
        try:
            filtered = [t for t in txns if (t.get("finalAmount") or 0) >= INSIDER_MIN_DOLLAR]
            if not filtered:
                continue

            total_dollar = sum(t.get("finalAmount", 0) or 0 for t in filtered)
            cluster_pts  = _cluster_score(filtered)
            dollar_pts   = _dollars_score(total_dollar)

            weighted_score = 0.0
            shares_amounts = 0
            latest_date = None
            for t in filtered:
                is_dir = bool(t.get("isDirector"))
                name   = t.get("name", "")
                title  = t.get("securitiesOwned", "")
                raw_senior = _seniority_score(is_dir, name, title)
                recency    = _recency_multiplier(t.get("transactionDate", ""), today)
                weighted_score += raw_senior * recency
                shares_amounts += float(t.get("sharesAmount", 0) or 0)
                if latest_date is None or (t.get("transactionDate", "") > latest_date):
                    latest_date = t.get("transactionDate", "")

            weighted_score += cluster_pts + dollar_pts
            n_txns = len(filtered)
            avg_score_per_txn = weighted_score / n_txns if n_txns else 0

            price = 0.0
            for t in filtered:
                p = t.get("price")
                if p and float(p) > 0:
                    price = float(p)
                    break
                amt = t.get("finalAmount")
                sha = t.get("sharesAmount")
                if amt and sha and float(sha) > 0:
                    price = float(amt) / float(sha)
                    break

            if price < INSIDER_MIN_PRICE:
                continue

            n_insiders = len({t.get("name", "") for t in filtered if t.get("name")})

            candidates.append({
                "symbol":          sym,
                "price":           round(price, 2),
                "insider_score":   round(weighted_score, 1),
                "avg_score_txn":   round(avg_score_per_txn, 1),
                "n_transactions":  n_txns,
                "n_insiders":      n_insiders,
                "total_dollar":    round(total_dollar, 0),
                "cluster_pts":     cluster_pts,
                "dollar_pts":     dollar_pts,
                "latest_date":     latest_date,
                "shares_bought":   shares_amounts,
            })
        except Exception as e:
            log.warning(f"Insider {sym}: %s", e)
            continue

    candidates.sort(key=lambda x: -x["insider_score"])
    top = candidates[:INSIDER_LIMIT]
    log.info(f"Insider: {len(top)}/{len(candidates)} candidates")
    for c in top:
        log.info(f"  {c['symbol']} score={c['insider_score']:.0f} "
                 f"txns={c['n_transactions']} insiders={c['n_insiders']} "
                 f"${c['total_dollar']:,.0f} latest={c['latest_date'][:10]}")
    return top
