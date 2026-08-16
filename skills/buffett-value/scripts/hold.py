#!/usr/bin/env python3
"""
Buffett Value Hold/Monitor Agent - ongoing position monitoring.

Re-screens open positions against the same fundamental criteria the analyst
agent used at entry, and flags thesis breaks or materially better
alternatives. There is no calendar-based exit: a position is only ever
flagged here for a fundamentals reason, never for time-in-trade.

Uses yfinance for fundamental data only (no FMP per user requirement).
"""

import logging
from typing import Any, Dict, List, Optional

from analyst import analyze_symbol

log = logging.getLogger(__name__)

# Relative-decline thresholds vs. the entry snapshot (not absolute re-screen failure)
ROE_DETERIORATION_PCT = 0.30          # ROE score fell >=30% relative to entry
DEBT_SCORE_DETERIORATION_PCT = 0.50   # Debt/Equity score fell >=50% relative to entry
MARGIN_OF_SAFETY_EROSION_PCT = 0.50   # Graham ratio discount shrank by >=50% relative to entry


def monitor_position(symbol: str, entry_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Re-run fundamental analysis for one open position and compare it to entry.

    Args:
        symbol: ticker to re-screen
        entry_snapshot: the candidate dict the analyst agent produced at entry
            (must contain 'fundamentals' and 'valuation' as returned by
            analyst.analyze_symbol)

    Returns:
        {symbol, current, thesis_intact, fundamentals_deteriorated,
         deterioration_reasons}
    """
    current = analyze_symbol(symbol)

    result: Dict[str, Any] = {
        "symbol": symbol,
        "current": current,
        "thesis_intact": True,
        "fundamentals_deteriorated": False,
        "deterioration_reasons": [],
    }

    if current is None:
        result["thesis_intact"] = False
        result["fundamentals_deteriorated"] = True
        result["deterioration_reasons"].append(
            "Unable to fetch current fundamentals (data unavailable)"
        )
        return result

    reasons = _compare_fundamentals(entry_snapshot, current)
    result["deterioration_reasons"] = reasons
    result["fundamentals_deteriorated"] = len(reasons) > 0
    result["thesis_intact"] = not result["fundamentals_deteriorated"]
    return result


def _compare_fundamentals(entry: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
    """Compare entry-time and current fundamentals; return thesis-break reasons."""
    reasons: List[str] = []

    entry_bq = entry.get("fundamentals", {}).get("business_quality", {}).get("details", {})
    current_bq = current.get("fundamentals", {}).get("business_quality", {}).get("details", {})

    entry_roe = entry_bq.get("ROE", {}).get("score")
    current_roe = current_bq.get("ROE", {}).get("score")
    if entry_roe and current_roe is not None:
        if current_roe <= entry_roe * (1 - ROE_DETERIORATION_PCT):
            reasons.append(
                f"ROE score dropped from {entry_roe:.2f} to {current_roe:.2f} "
                f"(>={ROE_DETERIORATION_PCT:.0%} relative decline)"
            )

    entry_debt = entry_bq.get("Debt/Equity", {}).get("score")
    current_debt = current_bq.get("Debt/Equity", {}).get("score")
    if entry_debt and current_debt is not None:
        # entry_debt > 0 guard: an entry score already at 0 has nowhere to
        # decline to, so entry_debt * (1 - pct) would also be 0 and any
        # still-zero current score would false-positive as "deterioration".
        if current_debt < entry_debt and current_debt <= entry_debt * (1 - DEBT_SCORE_DETERIORATION_PCT):
            reasons.append(
                f"Debt/Equity score dropped from {entry_debt:.2f} to {current_debt:.2f} "
                f"(debt load increased materially)"
            )

    entry_passed_quality = entry.get("criteria_met", {}).get("business_quality")
    current_passed_quality = current.get("criteria_met", {}).get("business_quality")
    if entry_passed_quality and current_passed_quality is False:
        reasons.append("Business quality no longer passes screen (was passing at entry)")

    entry_graham_ratio = entry.get("valuation", {}).get("valuation_details", {}).get("graham_ratio")
    current_graham_ratio = current.get("valuation", {}).get("valuation_details", {}).get("graham_ratio")
    if entry_graham_ratio and current_graham_ratio is not None:
        if current_graham_ratio <= 1.0:
            reasons.append(
                f"Margin of safety gone: Graham ratio fell to {current_graham_ratio:.2f} "
                f"(price >= fair value)"
            )
        else:
            # Compare the margin-of-safety *discount* (ratio - 1.0), not the raw
            # ratio -- a ratio-based threshold is unreachable for realistic entry
            # ratios (typically 1.0-2.0), since entry * 0.5 rarely exceeds 1.0.
            entry_discount = entry_graham_ratio - 1.0
            current_discount = current_graham_ratio - 1.0
            if entry_discount > 0 and current_discount <= entry_discount * (1 - MARGIN_OF_SAFETY_EROSION_PCT):
                reasons.append(
                    f"Margin of safety eroded: Graham ratio fell from {entry_graham_ratio:.2f} "
                    f"to {current_graham_ratio:.2f}"
                )

    return reasons


def find_better_opportunity(
    current_symbol: str,
    current_conviction: float,
    candidate_pool: List[Dict[str, Any]],
    min_conviction_edge: float = 0.15,
) -> Optional[Dict[str, Any]]:
    """
    Flag a materially better margin-of-safety opportunity among fresh analyst
    candidates than the current position offers.

    Returns the strongest alternative candidate if its conviction_score beats
    the current position by at least min_conviction_edge, else None.
    """
    better = [
        c
        for c in candidate_pool
        if c.get("symbol") != current_symbol
        and c.get("conviction_score", 0.0) >= current_conviction + min_conviction_edge
    ]
    if not better:
        return None
    return max(better, key=lambda c: c.get("conviction_score", 0.0))


def monitor_portfolio(
    positions: List[Dict[str, Any]],
    candidate_pool: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Monitor all open Buffett Value positions.

    Args:
        positions: [{symbol, entry_snapshot, ...}, ...]
        candidate_pool: optional fresh analyst candidates, checked for
            materially better alternatives to each held position

    Returns:
        One monitor result per position (see monitor_position), each
        annotated with 'better_opportunity'. No result is ever derived
        from time-in-trade -- only from fundamentals or a better alternative.
    """
    results = []
    for position in positions:
        symbol = position["symbol"]
        entry_snapshot = position.get("entry_snapshot", {})

        result = monitor_position(symbol, entry_snapshot)

        better_opportunity = None
        if candidate_pool:
            better_opportunity = find_better_opportunity(
                symbol,
                entry_snapshot.get("conviction_score", 0.0),
                candidate_pool,
            )
        result["better_opportunity"] = better_opportunity

        if result["fundamentals_deteriorated"]:
            log.info(f"{symbol}: thesis break flagged - {result['deterioration_reasons']}")
        if better_opportunity:
            log.info(
                f"{symbol}: better opportunity found - {better_opportunity['symbol']} "
                f"(conviction {better_opportunity['conviction_score']:.2f})"
            )

        results.append(result)

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Buffett Value Hold/Monitor Agent")
    print("Run via pytest for real coverage: skills/buffett-value/scripts/tests/test_hold.py")
