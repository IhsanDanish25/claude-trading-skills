"""
Insider-buying screener — FMP /stable/insider-trading P-Purchase signals.

Scores by executive seniority (CEO/CFO/COO > VP/Director > Other),
cluster count (multiple insiders buying), and dollar value.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from core.fmp import _get, _STABLE

log = logging.getLogger(__name__)

_SENIORITY_SCORES = {
    "CEO": 10,
    "CFO": 9,
    "COO": 8,
    "CTO": 7,
    "President": 7,
    "EVP": 6,
    "SVP": 5,
    "VP": 4,
    "Director": 3,
    "Officer": 3,
    "General Counsel": 4,
    "10% Owner": 6,
}


def _title_score(title: str) -> int:
    if not title:
        return 1
    upper = title.upper()
    for key, score in _SENIORITY_SCORES.items():
        if key.upper() in upper:
            return score
    return 1


def screen_insider(limit: int = 500) -> list[dict]:
    """Screen recent insider P-Purchase filings from FMP.

    Scoring formula:
      score = max_seniority * 3 + cluster_count * 5 + log_dollar_value_tier

    Returns candidates sorted by composite score descending.
    """
    data = _get(f"{_STABLE}/insider-trading", {"limit": limit})
    if not data or not isinstance(data, list):
        log.info("Insider screen: no data from FMP")
        return []

    purchases = [
        row for row in data
        if isinstance(row, dict) and row.get("transactionType") == "P-Purchase"
    ]

    if not purchases:
        log.info("Insider screen: no P-Purchase transactions found")
        return []

    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for row in purchases:
        sym = row.get("symbol")
        if sym:
            by_symbol[sym].append(row)

    candidates = []
    for sym, filings in by_symbol.items():
        titles = [f.get("reportingName", "") or f.get("typeOfOwner", "") for f in filings]
        seniority_scores = [_title_score(t) for t in titles]
        max_seniority = max(seniority_scores) if seniority_scores else 1
        cluster_count = len(filings)

        total_value = sum(
            abs(float(f.get("securitiesTransacted", 0) or 0) * float(f.get("price", 0) or 0))
            for f in filings
        )

        if total_value >= 10_000_000:
            dollar_tier = 5
        elif total_value >= 1_000_000:
            dollar_tier = 4
        elif total_value >= 500_000:
            dollar_tier = 3
        elif total_value >= 100_000:
            dollar_tier = 2
        else:
            dollar_tier = 1

        score = max_seniority * 3 + cluster_count * 5 + dollar_tier

        latest = filings[0]
        candidates.append({
            "symbol": sym,
            "price": float(latest.get("price", 0) or 0),
            "cluster_count": cluster_count,
            "max_seniority": max_seniority,
            "top_title": titles[seniority_scores.index(max_seniority)] if titles else "",
            "total_value": round(total_value, 2),
            "dollar_tier": dollar_tier,
            "score": score,
            "filing_date": latest.get("filingDate", ""),
            "strategy": "insider",
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info("Insider screen: %d symbols with P-Purchase from %d filings",
             len(candidates), len(purchases))
    return candidates
