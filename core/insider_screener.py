"""
Insider-purchase cluster screener — FMP /stable/insider-trading.

Filters to open-market buys (P-Purchase), aggregates by symbol over a rolling
lookback window, and scores candidates on three dimensions:
  1. Seniority  — CEO/CFO purchases carry the most weight.
  2. Cluster    — multiple distinct insiders buying the same stock (conviction signal).
  3. Dollar value — total notional of cluster purchases.

Data source: FMP /stable/insider-trading (via core.fmp.get_insider_trading).
Universe: same 80-stock curated list used by meanrev_screener.
"""
from __future__ import annotations

import datetime
import logging

log = logging.getLogger(__name__)

# Reuse the same curated 80-name universe as meanrev for consistency.
from core.meanrev_screener import MEANREV_UNIVERSE as INSIDER_UNIVERSE  # noqa: E402

_SENIORITY_KEYWORDS: list[tuple[list[str], int]] = [
    (["chief executive", "ceo"],         30),
    (["chief financial", "cfo"],         28),
    (["chief operating", "coo"],         22),
    (["president"],                      20),
    (["chief",],                         18),
    (["executive vice", "evp"],          15),
    (["senior vice", "svp"],             12),
    (["vice president", "vp"],           10),
    (["director"],                        8),
    (["10 percent", "10% owner"],         6),
]


def _seniority_score(type_of_owner: str) -> int:
    """Return a seniority score (0–30) from FMP's typeOfOwner field."""
    title = (type_of_owner or "").lower()
    for keywords, pts in _SENIORITY_KEYWORDS:
        if any(kw in title for kw in keywords):
            return pts
    return 4   # other officer / unknown


def _cluster_score(n_distinct_insiders: int) -> int:
    """0–50 pts — each unique insider in the cluster adds 10 pts, capped at 5."""
    return min(n_distinct_insiders, 5) * 10


def _dollar_score(total_dollars: float) -> int:
    """0–20 pts — scales with total notional up to $5M ceiling."""
    return min(int(total_dollars / 250_000), 20)


def _aggregate_symbol(rows: list[dict], from_date: str) -> dict | None:
    """Aggregate insider purchase rows for one symbol within the lookback window."""
    cluster: list[dict] = []
    for row in rows:
        tx_date = str(row.get("transactionDate") or row.get("filingDate") or "")
        if tx_date < from_date:
            continue
        price_raw = row.get("price") or row.get("securitiesPrice") or 0
        qty_raw   = row.get("securitiesTransacted") or row.get("shares") or 0
        try:
            tx_price = float(price_raw)
            tx_qty   = float(qty_raw)
        except (TypeError, ValueError):
            continue
        cluster.append({
            "name":    row.get("reportingName") or row.get("name") or "unknown",
            "title":   row.get("typeOfOwner") or row.get("officerTitle") or "",
            "dollars": tx_price * tx_qty,
            "date":    tx_date,
        })

    if not cluster:
        return None

    distinct_insiders = len({c["name"] for c in cluster})
    total_dollars     = sum(c["dollars"] for c in cluster)
    max_seniority     = max(_seniority_score(c["title"]) for c in cluster)
    latest_date       = max(c["date"] for c in cluster)

    score = max_seniority + _cluster_score(distinct_insiders) + _dollar_score(total_dollars)

    return {
        "cluster_count":    distinct_insiders,
        "total_dollars":    round(total_dollars),
        "max_seniority":    max_seniority,
        "latest_date":      latest_date,
        "score":            score,
        "cluster":          cluster,
    }


def screen(
    symbols: list[str] | None = None,
    lookback_days: int = 30,
    min_score: int = 20,
    min_cluster: int = 1,
) -> list[dict]:
    """Scan *symbols* for CEO/CFO-led insider-purchase clusters.

    Returns candidates sorted by score (highest first):
        {symbol, cluster_count, total_dollars, max_seniority, latest_date,
         score, cluster}
    """
    from core.fmp import get_insider_trading

    syms = symbols if symbols is not None else INSIDER_UNIVERSE
    from_date = (
        datetime.date.today() - datetime.timedelta(days=lookback_days)
    ).isoformat()

    log.info("Insider screen: %d symbols, lookback=%dd, min_score=%d", len(syms), lookback_days, min_score)

    candidates: list[dict] = []
    for sym in syms:
        try:
            rows = get_insider_trading(symbol=sym, transaction_type="P-Purchase", limit=50)
            if not rows:
                continue
            agg = _aggregate_symbol(rows, from_date)
            if agg is None:
                continue
            if agg["score"] < min_score:
                continue
            if agg["cluster_count"] < min_cluster:
                continue
            candidates.append({"symbol": sym, **agg})
        except Exception as e:
            log.debug("Insider %s skip: %s", sym, e)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info("Insider found %d candidates", len(candidates))
    return candidates
