"""
Insider-buying screener — FMP /stable/insider-trading.

Scores P-Purchase filings by executive seniority, cluster count (multiple
insiders buying within 14 days), and total dollar value.
"""
from __future__ import annotations

import datetime
import logging
from collections import defaultdict

from core.fmp import _get, _STABLE

log = logging.getLogger(__name__)

_SENIORITY = {
    "ceo": 5, "chief executive officer": 5,
    "cfo": 4, "chief financial officer": 4,
    "coo": 4, "chief operating officer": 4,
    "president": 4,
    "cto": 3, "chief technology officer": 3,
    "evp": 3, "executive vice president": 3,
    "svp": 2, "senior vice president": 2,
    "vp": 2, "vice president": 2,
    "director": 1, "10% owner": 1,
}

CLUSTER_WINDOW_DAYS = 14
LOOKBACK_DAYS = 30


def _title_score(title: str) -> int:
    if not title:
        return 0
    t = title.lower()
    for key, score in _SENIORITY.items():
        if key in t:
            return score
    return 0


def screen_insider(
    lookback_days: int = LOOKBACK_DAYS,
    min_total_value: float = 50_000,
    min_score: float = 3.0,
) -> list[dict]:
    """Screen for recent insider P-Purchase clusters."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=lookback_days)

    data = _get(f"{_STABLE}/insider-trading", {
        "transactionType": "P-Purchase",
        "page": "0",
    })

    if not isinstance(data, list) or not data:
        log.info("Insider screen: no data returned")
        return []

    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for row in data:
        filing_date = row.get("filingDate", "")
        if filing_date < start.isoformat() or filing_date > end.isoformat():
            continue
        sym = row.get("symbol")
        if not sym:
            continue
        by_symbol[sym].append(row)

    candidates = []
    for sym, filings in by_symbol.items():
        cluster_count = len(filings)
        total_value = sum(
            abs(float(f.get("securitiesTransacted", 0)) * float(f.get("price", 0)))
            for f in filings
        )
        if total_value < min_total_value:
            continue

        best_seniority = max(_title_score(f.get("reportingName", "")) for f in filings)

        score = (
            best_seniority * 2.0
            + min(cluster_count, 5) * 1.5
            + min(total_value / 500_000, 5.0)
        )

        if score < min_score:
            continue

        candidates.append({
            "symbol": sym,
            "score": round(score, 2),
            "cluster_count": cluster_count,
            "total_value": round(total_value, 2),
            "best_seniority": best_seniority,
            "filings": [
                {
                    "name": f.get("reportingName", ""),
                    "date": f.get("filingDate", ""),
                    "shares": f.get("securitiesTransacted"),
                    "price": f.get("price"),
                }
                for f in filings[:5]
            ],
            "strategy": "insider",
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info("Insider screen: %d candidates from %d symbols", len(candidates), len(by_symbol))
    return candidates
