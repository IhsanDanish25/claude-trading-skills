"""
Insider screener — SEC EDGAR Form 4 (primary) + FMP /stable/ (fallback).

P-Purchase = "Purchase" transactions filed on Form 4:
  - Directors buying their own stock (no banker's pitch, pure conviction)
  - Scored on: CEO/CFO seniority, cluster count, dollar value relative to market cap,
    and recency weighting.

Sources:
  1. SEC EDGAR (primary) — 0 API key, unlimited calls, fetches directly from sec.gov
  2. FMP /stable/insider-trading (fallback) — only if EDGAR returns nothing

FMP drain: 0 FMP calls per run under normal conditions.
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
from core.edgar import get_insider_transactions

log = logging.getLogger(__name__)

# In-process cache — survives across screen() calls within the same run.
_all_transactions_cache: list[dict] | None = None

# Seniority multipliers: CEO=most signal, CFO=strong, Director=moderate
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

    Sources (in order):
      1. SEC EDGAR — Form 4 XML directly from sec.gov (primary, zero FMP cost)
      2. FMP /stable/insider-trading — only if EDGAR returns nothing

    Both results are in-process cached for the rest of the run.
    """
    global _all_transactions_cache
    today = datetime.date.today()

    # ── Transaction fetch (EDGAR primary, FMP fallback) ─────────────────────
    if _all_transactions_cache is not None:
        all_transactions = _all_transactions_cache
        log.info(f"  Using in-process cache ({len(all_transactions)} transactions)")

    else:
        # 1. SEC EDGAR (0 FMP calls, 0 API keys)
        log.info("Insider: pulling Form 4 P-Purchases from SEC EDGAR")
        try:
            all_transactions = get_insider_transactions()
        except Exception as e:
            log.warning(f"EDGAR failed: {e} — falling back to FMP")
            all_transactions = []

        if all_transactions:
            _all_transactions_cache = all_transactions
            log.info(f"  EDGAR → {len(all_transactions)} transactions")
        else:
            # 2. FMP fallback (only if EDGAR came up empty)
            if fmp_remaining_calls() >= 1:
                cutoff = today - datetime.timedelta(days=INSIDER_LOOKBACK_DAYS)
                log.info("Insider: EDGAR empty — trying FMP /stable/insider-trading")
                try:
                    data = _get(f"{_stable}/insider-trading", {
                        "from": cutoff.isoformat(),
                        "to":   today.isoformat(),
                    })
                    all_transactions = data if isinstance(data, list) else []
                except Exception as e:
                    log.warning(f"FMP fallback failed: {e}")
                    all_transactions = []
                if all_transactions:
                    _all_transactions_cache = all_transactions
                    log.info(f"  FMP → {len(all_transactions)} transactions")
            else:
                log.info("Insider: no transactions (EDGAR empty, FMP budget exhausted)")

    # ── Filter to P-Purchases ──────────────────────────────────────────────────
    purchases = [
        t for t in all_transactions
        if isinstance(t, dict) and t.get("transactionType") == "P-Purchase"
    ]
    log.info(f"  P-Purchases available: {len(purchases)}")

    # ── Aggregate by symbol ────────────────────────────────────────────────────
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for t in purchases:
        sym = t.get("symbol")
        if not sym:
            continue
        by_symbol[sym].append(t)

    # ── Score each symbol ──────────────────────────────────────────────────────
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
            shares_amounts = 0.0
            latest_date = None
            for t in filtered:
                is_dir = bool(t.get("isDirector"))
                name   = t.get("name", "")
                title  = t.get("securitiesOwned", "")
                raw_senior = _seniority_score(is_dir, name, title)
                recency    = _recency_multiplier(t.get("transactionDate", ""), today)
                weighted_score += raw_senior * recency
                shares_amounts += float(t.get("sharesAmount") or 0)
                tdate = t.get("transactionDate", "")
                if tdate and (latest_date is None or tdate > latest_date):
                    latest_date = tdate

            weighted_score += cluster_pts + dollar_pts
            n_txns = len(filtered)
            avg_score = weighted_score / n_txns if n_txns else 0.0

            # Price from transaction data
            price = 0.0
            for t in filtered:
                p = t.get("price")
                if p and float(p) > 0:
                    price = float(p)
                    break
                amt = t.get("finalAmount")
                sha = t.get("sharesAmount")
                if amt and sha and float(sha) > 0:
                    try:
                        price = float(amt) / float(sha)
                        break
                    except (ValueError, TypeError, ZeroDivisionError):
                        pass

            if price < INSIDER_MIN_PRICE:
                continue

            n_insiders = len({t.get("name", "") for t in filtered if t.get("name")})

            candidates.append({
                "symbol":         sym,
                "price":          round(price, 2),
                "insider_score":  round(weighted_score, 1),
                "avg_score_txn":  round(avg_score, 1),
                "n_transactions": n_txns,
                "n_insiders":     n_insiders,
                "total_dollar":   round(total_dollar, 0),
                "cluster_pts":    cluster_pts,
                "dollar_pts":    dollar_pts,
                "latest_date":    latest_date,
                "shares_bought":  round(shares_amounts, 0),
            })
        except Exception as e:
            log.warning(f"Insider {sym}: skip (scoring error: {e})")
            continue

    candidates.sort(key=lambda x: -x["insider_score"])
    top = candidates[:INSIDER_LIMIT]
    log.info(f"Insider: {len(top)}/{len(candidates)} BUY candidates")
    for c in top:
        log.info(f"  ★ {c['symbol']} score={c['insider_score']:.0f} "
                 f"${c['total_dollar']:,.0f} {c['n_transactions']} txns "
                 f"latest={str(c['latest_date'])[:10]}")
    return top
