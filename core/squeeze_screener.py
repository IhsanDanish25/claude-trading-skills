"""
Short-squeeze candidate screener — FMP /stable/short-interest.

Qualification gates:
  • Short interest ≥ SI_MIN_PCT  (default 15% of float)
  • Days-to-cover  ≥ DTC_MIN     (default 3 days)
  • Positive momentum             (1-month price return > 0)

Scoring: weighted combination of SI%, DTC, and 1-month return.

Data sources:
  • Short interest — core.fmp.get_short_interest  → /stable/short-interest
  • Price / momentum — core.fmp.get_daily_bars    → /stable/historical-price-eod/full
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Universe skewed toward mid-cap growth / biotech / high-short-interest sectors.
SQUEEZE_UNIVERSE: list[str] = [
    "GME", "AMC", "BBBY", "MARA", "RIOT", "CLSK", "CIFR", "HUT", "ARBK",
    "UPST", "AFRM", "LCID", "RIVN", "NKLA", "BLNK", "CHPT", "PLUG", "FCEL",
    "BYND", "SPCE", "OPEN", "CLOV", "WISH", "WKHS", "RKT", "PTON", "PRTY",
    "CNVS", "BBAI", "SES", "EVGO", "XPEV", "LI", "NIO", "GOEV", "RIDE",
    "LAZR", "LIDR", "MVIS", "PDCO", "CVNA", "DKNG", "HOOD", "RBLX",
    "PENN", "SFIX", "VIRT", "MRNA", "BNTX", "ARWR", "IONS", "EDIT",
    "NTLA", "BEAM", "CRSP", "PACB", "TDOC", "ACMR", "ASAN", "HIMS",
    "JOBY", "ARCHER", "LILM", "VFS", "PSNY", "PAYO", "BILL", "DOCN",
    "FRSH", "AMPL", "SEMR", "MNDY", "GTLB", "SMAR", "APPN", "ESTC",
    "DDOG", "NET", "SNOW", "ZS", "CRWD", "PANW", "CYBR", "TENB",
]


# ── Field-name adapters (FMP field names vary across plan tiers) ──────────────

def _extract_si_pct(row: dict) -> float | None:
    for key in (
        "shortPercentOfFloat", "shortPercentFloat",
        "shortInterestPercent", "percentOfFloatShort",
        "shortInterestRatio",   # sometimes this is SI % expressed as ratio * 100
    ):
        val = row.get(key)
        if val is not None:
            try:
                f = float(val)
                # Some plans return 0–1 ratio; normalise to percent if < 2
                return f * 100.0 if f < 2.0 else f
            except (TypeError, ValueError):
                continue
    return None


def _extract_dtc(row: dict, avg_volume: float | None = None) -> float | None:
    for key in ("daysToCover", "shortRatio", "daysToConverAll"):
        val = row.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    # Derive from shortInterest / avgVolume when not available directly
    si_raw = row.get("shortInterest") or row.get("shortVolume")
    if si_raw is not None and avg_volume:
        try:
            return float(si_raw) / avg_volume
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return None


def _momentum_1m(bars: list[dict]) -> float | None:
    """1-month return % from newest-first daily bars."""
    if len(bars) < 22:
        return None
    try:
        return round((bars[0]["close"] - bars[21]["close"]) / bars[21]["close"] * 100, 2)
    except (KeyError, ZeroDivisionError, TypeError):
        return None


def _score_squeeze(si_pct: float, dtc: float, momentum: float) -> int:
    """0–100 composite squeeze score."""
    si_pts   = min(int((si_pct - 15.0) * 2), 40)    # 0–40 for SI 15→35%+
    dtc_pts  = min(int((dtc - 3.0) * 5), 30)         # 0–30 for DTC 3→9+
    mom_pts  = min(int(max(momentum, 0.0) * 2), 30)  # 0–30 for 0→15% 1-mo return
    return max(0, si_pts + dtc_pts + mom_pts)


# ── Public screen function ────────────────────────────────────────────────────

def screen(
    symbols: list[str] | None = None,
    si_min_pct: float = 15.0,
    dtc_min: float = 3.0,
    min_price: float = 3.0,
    min_avg_volume: float = 500_000.0,
) -> list[dict]:
    """Screen for short-squeeze setups with positive momentum.

    Returns candidates sorted by score (highest first):
        {symbol, si_pct, dtc, momentum_1m_pct, avg_volume, price, score}
    """
    from core.fmp import get_daily_bars, get_short_interest

    syms = symbols if symbols is not None else SQUEEZE_UNIVERSE
    log.info("Squeeze screen: %d symbols, SI≥%.0f%%, DTC≥%.1f", len(syms), si_min_pct, dtc_min)

    candidates: list[dict] = []
    for sym in syms:
        try:
            si_rows = get_short_interest(symbol=sym, limit=10)
            if not si_rows:
                continue
            row = si_rows[0]   # most recent

            # Must have price / bar data anyway for momentum & volume
            bars = get_daily_bars(sym, days=60)
            if not bars:
                continue
            price = float(bars[0]["close"])
            if price < min_price:
                continue
            vols     = [float(b.get("volume", 0)) for b in bars[:20]]
            avg_vol  = sum(vols) / len(vols) if vols else 0.0
            if avg_vol < min_avg_volume:
                continue

            si_pct = _extract_si_pct(row)
            if si_pct is None or si_pct < si_min_pct:
                continue

            dtc = _extract_dtc(row, avg_vol)
            if dtc is None or dtc < dtc_min:
                continue

            mom = _momentum_1m(bars)
            if mom is None or mom <= 0.0:
                continue   # require positive momentum for a squeeze setup

            score = _score_squeeze(si_pct, dtc, mom)
            candidates.append({
                "symbol":        sym,
                "si_pct":        round(si_pct, 2),
                "dtc":           round(dtc, 2),
                "momentum_1m_pct": mom,
                "avg_volume":    round(avg_vol),
                "price":         round(price, 2),
                "score":         score,
            })
        except Exception as e:
            log.debug("Squeeze %s skip: %s", sym, e)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info("Squeeze found %d candidates", len(candidates))
    return candidates
