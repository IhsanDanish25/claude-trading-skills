"""
Short-squeeze screener — FMP /stable/short-interest data.

Criteria: SI > 15%, days-to-cover > 3, positive momentum (price above SMA20).
"""
from __future__ import annotations

import logging

from core.fmp import _get, _STABLE

log = logging.getLogger(__name__)

_MIN_SI_PCT = 15.0
_MIN_DTC = 3.0
_SMA_PERIOD = 20


def screen_squeeze() -> list[dict]:
    """Screen for short-squeeze candidates using FMP short-interest data.

    Criteria:
    - Short interest > 15% of float
    - Days to cover > 3
    - Price above 20-day SMA (positive momentum)

    Returns candidates sorted by days-to-cover descending (highest squeeze potential first).
    """
    data = _get(f"{_STABLE}/short-interest", {})
    if not data or not isinstance(data, list):
        log.info("Squeeze screen: no short-interest data from FMP")
        return []

    high_si = []
    for row in data:
        if not isinstance(row, dict):
            continue
        si_pct = float(row.get("shortInterestPercentOfFloat", 0) or
                       row.get("shortPercentOfFloat", 0) or 0)
        dtc = float(row.get("daysToCover", 0) or 0)
        sym = row.get("symbol")
        if not sym or si_pct < _MIN_SI_PCT or dtc < _MIN_DTC:
            continue
        high_si.append({
            "symbol": sym,
            "si_pct": si_pct,
            "days_to_cover": dtc,
            "short_interest": int(row.get("shortInterest", 0) or 0),
            "date": row.get("date", ""),
        })

    if not high_si:
        log.info("Squeeze screen: no stocks with SI>%.0f%% and DTC>%.0f",
                 _MIN_SI_PCT, _MIN_DTC)
        return []

    symbols_to_check = [r["symbol"] for r in high_si]
    price_data = _fetch_momentum(symbols_to_check)

    candidates = []
    for row in high_si:
        sym = row["symbol"]
        pm = price_data.get(sym)
        if not pm:
            continue
        if pm["price"] <= pm["sma20"]:
            continue

        momentum_pct = round((pm["price"] - pm["sma20"]) / pm["sma20"] * 100, 2)
        score = round(row["si_pct"] * 2 + row["days_to_cover"] * 5 + momentum_pct, 1)

        candidates.append({
            **row,
            "price": round(pm["price"], 2),
            "sma20": round(pm["sma20"], 2),
            "momentum_pct": momentum_pct,
            "score": score,
            "strategy": "squeeze",
        })

    candidates.sort(key=lambda x: x["days_to_cover"], reverse=True)
    log.info("Squeeze screen: %d candidates from %d high-SI stocks",
             len(candidates), len(high_si))
    return candidates


def _fetch_momentum(symbols: list[str]) -> dict[str, dict]:
    """Fetch price + SMA20 for momentum confirmation via FMP bars."""
    out: dict[str, dict] = {}
    for sym in symbols:
        try:
            bars = _get(f"{_STABLE}/historical-price-eod/full", {"symbol": sym})
            if not bars or not isinstance(bars, list) or len(bars) < _SMA_PERIOD:
                continue
            closes = [b["close"] for b in bars[:_SMA_PERIOD] if b.get("close")]
            if len(closes) < _SMA_PERIOD:
                continue
            price = closes[0]
            sma20 = sum(closes) / len(closes)
            out[sym] = {"price": price, "sma20": sma20}
        except Exception as e:
            log.warning("squeeze momentum %s: %s", sym, e)
    return out
