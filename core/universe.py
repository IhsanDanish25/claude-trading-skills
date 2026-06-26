from __future__ import annotations
"""
DYNAMIC UNIVERSE — Alpaca-native candidate sourcing (no FMP, no rate limits).

The market_open loop historically screened a fixed 30-name watchlist, so the
"best trade" could only ever be one of those 30. This module widens the net to
the actual market each day by pulling Alpaca's most-active and biggest-moving
liquid stocks (free IEX screener), unioned with the curated watchlist as a seed.

Robustness contract: build_universe() NEVER raises and NEVER returns empty.
On any Alpaca error it falls back to the static WATCHLIST, so the live loop is
strictly no-worse than before. Junk (leveraged/inverse ETFs, bad tickers) is
filtered here; liquidity/price-band filtering still happens downstream in the
screener (avg_vol >= 100k, price 5-500), so this stays permissive.
"""
import logging
import os
import re

from core.config import WATCHLIST

log = logging.getLogger(__name__)

USE_DYNAMIC_UNIVERSE = os.environ.get("USE_DYNAMIC_UNIVERSE", "true").lower() == "true"
UNIVERSE_MAX = int(os.environ.get("UNIVERSE_MAX", "100"))   # cap total symbols screened
ACTIVES_TOP  = int(os.environ.get("UNIVERSE_ACTIVES_TOP", "60"))
MOVERS_TOP   = int(os.environ.get("UNIVERSE_MOVERS_TOP", "20"))

# Common leveraged / inverse / volatility ETPs that show up in movers and
# actives but are not the kind of single-name momentum trade this loop wants.
_ETP_BLOCKLIST = {
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA", "SPXL", "SPXS", "SPXU",
    "UPRO", "SDOW", "UDOW", "TMF", "TMV", "LABU", "LABD", "FAS", "FAZ",
    "UVXY", "VXX", "SVXY", "VIXY", "YANG", "YINN", "NUGT", "DUST", "JNUG",
    "JDST", "BOIL", "KOLD", "UCO", "SCO", "ERX", "ERY", "WEBL", "WEBS",
    "TSLL", "TSLQ", "NVDL", "NVDU", "NVDD", "BITX", "ETHU", "MSTX", "MSTU",
    "USD", "SSO", "QLD", "DDM", "ROM", "AGQ", "UGL", "GLL",
}
# Reject obviously non-common-stock tickers (warrants, units, rights, prefs).
_BAD_SUFFIX = re.compile(r"[.\-/](W|WS|U|R|RT|P|PR[A-Z]?)$", re.IGNORECASE)


def _clean_universe(symbols: list[str], seed: list[str] | None = None,
                    cap: int = UNIVERSE_MAX) -> list[str]:
    """Pure: dedup, drop blocklisted ETPs / malformed tickers, cap length.
    Seed symbols are always kept and placed first (curated watchlist)."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(sym: str) -> None:
        if not sym:
            return
        s = sym.strip().upper()
        if not s or s in seen:
            return
        if s in _ETP_BLOCKLIST or _BAD_SUFFIX.search(s):
            return
        if not re.fullmatch(r"[A-Z]{1,5}", s):  # plain US equity tickers only
            return
        seen.add(s)
        out.append(s)

    for s in (seed or []):
        _add(s)
    for s in symbols:
        _add(s)
        if len(out) >= cap:
            break
    return out[:cap]


def _fetch_alpaca_movers() -> list[str]:
    """Most-active + top gainers via the Alpaca screener (lazy import so this
    module is importable without alpaca). Returns [] on any failure."""
    try:
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MostActivesRequest, MarketMoversRequest
        from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY
    except Exception as e:                       # noqa: BLE001
        log.warning("universe: alpaca screener import failed (%s)", e)
        return []

    client = ScreenerClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    syms: list[str] = []

    try:
        actives = client.get_most_actives(MostActivesRequest(top=ACTIVES_TOP))
        for a in getattr(actives, "most_actives", []) or []:
            s = getattr(a, "symbol", None)
            if s:
                syms.append(s)
    except Exception as e:                       # noqa: BLE001
        log.warning("universe: get_most_actives failed (%s)", e)

    try:
        movers = client.get_market_movers(MarketMoversRequest(top=MOVERS_TOP))
        for g in getattr(movers, "gainers", []) or []:
            s = getattr(g, "symbol", None)
            if s:
                syms.append(s)
    except Exception as e:                       # noqa: BLE001
        log.warning("universe: get_market_movers failed (%s)", e)

    return syms


def build_universe() -> list[str]:
    """Build the day's screening universe. Always non-empty; never raises.

    Dynamic = curated WATCHLIST (seed) ∪ Alpaca most-actives ∪ top gainers,
    cleaned and capped. Falls back to WATCHLIST alone if dynamic is disabled or
    Alpaca returns nothing."""
    if not USE_DYNAMIC_UNIVERSE:
        log.info("universe: dynamic disabled — using static watchlist (%d)", len(WATCHLIST))
        return list(WATCHLIST)

    try:
        raw = _fetch_alpaca_movers()
    except Exception as e:                       # noqa: BLE001 — belt-and-suspenders
        log.warning("universe: dynamic fetch crashed (%s) — falling back to watchlist", e)
        raw = []

    universe = _clean_universe(raw, seed=list(WATCHLIST))
    dynamic_added = len(universe) - sum(1 for s in WATCHLIST if s in universe)
    if not raw:
        log.warning("universe: Alpaca screener returned nothing — watchlist only (%d)", len(universe))
    else:
        log.info("universe: %d symbols (watchlist seed + %d dynamic Alpaca names)",
                 len(universe), max(dynamic_added, 0))
    return universe
