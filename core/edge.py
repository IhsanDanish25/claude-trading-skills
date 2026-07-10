"""
Edge module — alpha-boosting filters layered on top of the VCP screen.

Six upgrades (pack 1):
  1. Entry timing      — is_entry_window(): block first N min after open
  2. FTD bottom catch  — defensive sizing on follow-through day
  3. Relative strength — rs_rating(): stock 1-month return vs SPY
  4. Sector rotation   — strong_sectors(): only trade leading sectors
  5. Volume confirm    — breakout_confirmed(): require 1.5x avg volume
  6. Partial profit    — handled in market_close/position review (config-driven)

Pack 2:
  7.  Gap guard         — gap_ok(): reject stocks gapping > MAX_GAP_PCT vs prior close
  8.  Earnings blackout — earnings_clear(): block entries within EARNINGS_BLACKOUT_DAYS
  9.  Sector cap        — sector_concentration_ok(): cap entries per sector
  10. Pyramiding        — should_pyramid(): add to winners past PYRAMID_TRIGGER_PCT
  11. Circuit breaker   — use CircuitBreaker from circuit_breaker.py (raises on halt)
  12. Intraday trail    — compute_trail_stop(): ratchet stop up, never down
"""
from __future__ import annotations

import datetime
import logging

import pytz

from core import config
from core.fmp import get_daily_bars, get_market_breadth, get_next_earnings

log = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


def is_entry_window(now: datetime.datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.datetime.now(ET)
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    earliest = open_t + datetime.timedelta(minutes=config.ENTRY_DELAY_MIN)
    close_t = now.replace(hour=15, minute=45, second=0, microsecond=0)
    if now < earliest:
        return False, f"too early — wait until {earliest.strftime('%H:%M')} ET ({config.ENTRY_DELAY_MIN}min after open)"
    if now > close_t:
        return False, "too late — within 15min of close, no new entries"
    return True, "entry window open"


def _return_pct(symbol: str, lookback: int = 21) -> float | None:
    bars = get_daily_bars(symbol, days=lookback + 5)
    if len(bars) < lookback:
        return None
    recent = bars[0]["close"]
    past = bars[lookback - 1]["close"]
    if past <= 0:
        return None
    return round((recent - past) / past * 100, 2)


def rs_rating(symbol: str, spy_return: float | None = None) -> float | None:
    if spy_return is None:
        spy_return = _return_pct("SPY")
    if spy_return is None:
        return None
    stock_return = _return_pct(symbol)
    if stock_return is None:
        return None
    return round(stock_return - spy_return, 2)


def passes_rs(symbol: str, spy_return: float | None = None) -> tuple[bool, float | None]:
    rs = rs_rating(symbol, spy_return)
    if rs is None:
        return True, None
    return rs >= config.MIN_RS_RATING, rs


def strong_sectors(top_n: int = 4) -> list[str]:
    breadth = get_market_breadth()
    sectors = breadth.get("sector_perf", {})
    if not sectors:
        return []
    ranked = sorted(sectors.items(), key=lambda x: x[1], reverse=True)
    return [name for name, _ in ranked[:top_n]]


def breakout_confirmed(symbol: str, current_volume: float) -> tuple[bool, float]:
    bars = get_daily_bars(symbol, days=25)
    vols = [b["volume"] for b in bars[:20] if b.get("volume")]
    if not vols:
        return True, 0.0
    avg_vol = sum(vols) / len(vols)
    rel = round(current_volume / avg_vol, 2) if avg_vol > 0 else 0.0
    return rel >= config.BREAKOUT_VOL_MULT, rel


def position_size_pct(regime_bias: str, ftd_detected: bool) -> float:
    if regime_bias == "aggressive":
        return config.MAX_POSITION_SIZE_PCT
    if regime_bias == "moderate":
        return config.MAX_POSITION_SIZE_PCT
    if regime_bias == "defensive":
        return config.MAX_POSITION_SIZE_PCT * 0.5
    if regime_bias == "cash":
        if ftd_detected and config.ALLOW_FTD_BOTTOM_BUY:
            log.info("FTD detected in cash regime — allowing defensive bottom-catch entry")
            return config.FTD_DEFENSIVE_SIZE
        return 0.0
    return 0.0


def apply_edge_filters(
    candidate: dict,
    regime_bias: str,
    ftd_detected: bool,
    spy_return: float | None,
    strong_sector_list: list[str],
) -> dict:
    reasons = []
    symbol = candidate["symbol"]

    size_pct = position_size_pct(regime_bias, ftd_detected)
    if size_pct <= 0:
        return {"pass": False, "size_pct": 0, "rs": None, "rel_vol": None,
                "reasons": ["regime blocks entry (cash, no FTD)"]}

    rs_ok, rs = passes_rs(symbol, spy_return)
    if not rs_ok:
        return {"pass": False, "size_pct": 0, "rs": rs, "rel_vol": None,
                "reasons": [f"RS {rs} below SPY+{config.MIN_RS_RATING}"]}
    reasons.append(f"RS {rs:+.1f}% vs SPY" if rs is not None else "RS n/a")

    if config.STRONG_SECTORS_ONLY and strong_sector_list:
        sector = candidate.get("sector", "")
        if sector and sector not in strong_sector_list:
            return {"pass": False, "size_pct": 0, "rs": rs, "rel_vol": None,
                    "reasons": [f"sector '{sector}' not in top performers"]}
        if sector:
            reasons.append(f"sector '{sector}' leading")

    rel_vol = candidate.get("rel_volume", 0)
    if rel_vol and rel_vol < config.BREAKOUT_VOL_MULT:
        return {"pass": False, "size_pct": 0, "rs": rs, "rel_vol": rel_vol,
                "reasons": [f"volume {rel_vol}x below {config.BREAKOUT_VOL_MULT}x req"]}
    reasons.append(f"vol {rel_vol}x confirmed")

    return {"pass": True, "size_pct": size_pct, "rs": rs, "rel_vol": rel_vol, "reasons": reasons}


# ── Edge pack 2 ───────────────────────────────────────────────────────────────

def gap_ok(symbol: str, live_price: float, max_gap_pct: float | None = None) -> tuple[bool, float | None]:
    """Reject names that have gapped more than MAX_GAP_PCT off the prior close —
    chasing an extended gap is poor risk/reward. Returns (ok, gap_pct)."""
    max_gap_pct = config.MAX_GAP_PCT if max_gap_pct is None else max_gap_pct
    bars = get_daily_bars(symbol, days=5)
    if not bars or not live_price:
        return True, None
    prior_close = bars[0].get("close", 0)
    if prior_close <= 0:
        return True, None
    gap = round((live_price - prior_close) / prior_close * 100, 2)
    return abs(gap) <= max_gap_pct, gap


def earnings_clear(symbol: str, days_buffer: int | None = None) -> tuple[bool, str | None]:
    """Block new entries when earnings fall within EARNINGS_BLACKOUT_DAYS —
    avoid holding a fresh breakout into a binary event. Returns (clear, date)."""
    days_buffer = config.EARNINGS_BLACKOUT_DAYS if days_buffer is None else days_buffer
    next_date = get_next_earnings(symbol)
    if not next_date:
        return True, None
    try:
        ed = datetime.datetime.strptime(next_date, "%Y-%m-%d").date()
    except ValueError:
        return True, next_date
    days_out = (ed - datetime.date.today()).days
    return days_out > days_buffer, next_date


def sector_concentration_ok(
    candidate_sector: str,
    open_positions: list[dict],
    max_per_sector: int | None = None,
) -> tuple[bool, int]:
    """Cap how many positions share one sector to keep the book diversified.
    open_positions is a list of dicts each carrying a 'sector' key.
    Returns (ok, current_count_in_sector)."""
    max_per_sector = config.MAX_PER_SECTOR if max_per_sector is None else max_per_sector
    if not candidate_sector:
        return True, 0
    count = sum(1 for p in open_positions if p.get("sector") == candidate_sector)
    return count < max_per_sector, count


def should_pyramid(position: dict) -> bool:
    """Add to a winner once it clears PYRAMID_TRIGGER_PCT, but only once.
    position carries 'pnl_pct' and a 'pyramided' flag."""
    if not config.ALLOW_PYRAMIDING:
        return False
    if position.get("pyramided"):
        return False
    return position.get("pnl_pct", 0) >= config.PYRAMID_TRIGGER_PCT * 100


def circuit_breaker_tripped(portfolio_value: float, day_start_value: float) -> bool:
    """
    DEPRECATED — routing to CircuitBreaker.check_before_order() instead.

    CircuitBreaker is instantiated in market_open and midday_review with
    day_start_equity from day_start_value.json, and raises TradingHalted
    on breach. This function is kept as a no-op shim to avoid import errors
    in any callers that still reference it (they should migrate to CircuitBreaker).
    """
    import warnings
    warnings.warn(
        "circuit_breaker_tripped is deprecated — use CircuitBreaker from circuit_breaker.py",
        DeprecationWarning, stacklevel=2,
    )
    if not day_start_value or day_start_value <= 0:
        return False
    day_pnl_pct = (portfolio_value - day_start_value) / day_start_value * 100
    return day_pnl_pct <= -config.CIRCUIT_BREAKER_PCT * 100


def compute_trail_stop(current_price: float, entry_price: float, current_stop: float) -> float:
    """Ratchet the stop up by TRAIL_STOP_PCT below price once in profit.
    Never returns a stop lower than the current one."""
    if current_price <= entry_price:
        return current_stop
    candidate = round(current_price * (1 - config.TRAIL_STOP_PCT), 2)
    return max(current_stop, candidate)
