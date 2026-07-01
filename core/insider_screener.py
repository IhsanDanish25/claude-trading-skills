"""
Insider screener — FMP /stable/insider-trading, transaction type: P-Purchase.

P-Purchase = "Purchase" transactions filed on Form 4 — the clearest insider signal:
  - Directors buying their own stock (no banker's pitch, pure conviction)
  - Scored on: CEO/CFO seniority, cluster count (multiple buys near same price),
    dollar value relative to market cap, and recency weighting.

Scoring formula (max 100 pts):
  - Seniority bonus:        CEO=40pts, CFO=30pts, other insider=10pts
  - Cluster bonus:        +3pts per additional same-week transaction (cap 30)
  - Dollar-value bonus:    +10pts if $500k+, +20pts if $5M+
  - Recency decay:         transactions older than 14 days get linear discount
  - Shares-out float adj:   normalize by shares outstanding

Pipeline:
  1. Fetch insider transactions via FMP /stable/insider-trading
  2. Filter to transactionType == "P-Purchase" in last N days
  3. Score each transaction; aggregate per symbol
  4. Return sorted by aggregate insider_score

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
from core.fmp import _get, _stable

log = logging.getLogger(__name__)

# Seniority multipliers: CEO=most signal, CFO=strong, VP/Director=moderate
_SENIORITY_WEIGHT = {
    "CEO":   40,
    "CFO":   30,
    "COO":   25,
    "President": 25,
    "Director":  10,
    "Chairman":  30,
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


def _recency_multiplier(transaction_date: str, cutoff: datetime.date) -> float:
    """Linear discount: 1.0 for today, 0.0 for older than LOOKBACK_DAYS."""
    try:
        d = datetime.date.fromisoformat(transaction_date[:10])
    except (ValueError, TypeError):
        return 0.0
    age = (cutoff - d).days
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


def _company_seniority(name: str) -> int:
    """Score based on name matching known-CEO/CFO pattern."""
    n = (name or "").upper()
    if "CEO" in n or "CHIEF EXECUTIVE" in n:
        return _SENIORITY_WEIGHT["CEO"]
    if "CFO" in n or "CHIEF FINANCIAL" in n:
        return _SENIORITY_WEIGHT["CFO"]
    if "PRESIDENT" in n:
        return _SENIORITY_WEIGHT["President"]
    if "DIRECTOR" in n and len(n) < 60:
        return _SENIORITY_WEIGHT["Director"]
    return 0


def screen() -> list[dict]:
    """
    Run insider purchase screen. Returns top-LIMIT candidates sorted by
    aggregate insider_score.
    """
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=INSIDER_LOOKBACK_DAYS)

    # Fetch all transactions for universe symbols in one batch call
    log.info(f"Insider screen: fetching P-Purchases via FMP /stable/ (last "
            f"{INSIDER_LOOKBACK_DAYS}d, universe={len(S&P80_UNIVERSE)})")

    all_transactions: list[dict] = []
    for sym in S&P80_UNIVERSE:
        try:
            data = _get(f"{_stable}/insider-trading", {
                "symbol":     sym,
                "from":       cutoff.isoformat(),
                "to":         today.isoformat(),
            })
            if not isinstance(data, list):
                continue
            all_transactions.extend(data)
        except Exception as e:
            log.debug("FMP insider %s: %s", sym, e)
            continue

    purchases = [
        t for t in all_transactions
        if isinstance(t, dict) and t.get("transactionType") == "P-Purchase"
    ]
    log.info(f"  Found {len(purchases)} P-Purchases in window")

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
            # Filter by minimum dollar threshold
            filtered = [t for t in txns if (t.get("finalAmount") or 0) >= INSIDER_MIN_DOLLAR]
            if not filtered:
                continue

            total_dollar = sum(t.get("finalAmount", 0) or 0 for t in filtered)

            # Aggregate scores
            cluster_pts = _cluster_score(filtered)
            dollar_pts = _dollars_score(total_dollar)

            weighted_score = 0.0
            shares_amounts = 0
            latest_date = None
            for t in filtered:
                is_dir = bool(t.get("isDirector"))
                name = t.get("name", "")
                title = t.get("securitiesOwned", "")
                raw_senior = _seniority_score(is_dir, name, title)
                recency = _recency_multiplier(t.get("transactionDate", ""), today)
                weight = raw_senior * recency
                weighted_score += weight
                shares_amounts += float(t.get("sharesAmount", 0) or 0)
                if latest_date is None or (t.get("transactionDate", "") > latest_date):
                    latest_date = t.get("transactionDate", "")

            # Add cluster and dollar bonuses
            weighted_score += cluster_pts + dollar_pts

            # Normalize by transaction count (avoid inflate from many tiny txns)
            n_txns = len(filtered)
            avg_score_per_txn = weighted_score / n_txns if n_txns else 0

            # Compute last price for the symbol
            price = 0.0
            for t in filtered:
                p = t.get("price")
                if p and float(p) > 0:
                    price = float(p)
                    break
                # Try to derive from finalAmount / sharesAmount
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
                "avg_score_txn":  round(avg_score_per_txn, 1),
                "n_transactions":  n_txns,
                "n_insiders":     n_insiders,
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
    log.info(f"Insider: {len(top)}/{len(candidates)} candidates by score")
    for c in top:
        log.info(f"  {c['symbol']} score={c['insider_score']:.0f} "
                 f"txns={c['n_transactions']} insiders={c['n_insiders']} "
                 f"${c['total_dollar']:,.0f} latest={c['latest_date'][:10]}")
    return top