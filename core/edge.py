"""
Edge module — alpha-boosting filters layered on top of the VCP screen.

Six upgrades:
  1. Entry timing      — is_entry_window(): block first N min after open
  2. FTD bottom catch  — defensive sizing on follow-through day
  3. Relative strength — rs_rating(): stock 1-month return vs SPY
  4. Sector rotation   — strong_sectors(): only trade leading sectors
  5. Volume confirm    — breakout_confirmed(): require 1.5x avg volume
  6. Partial profit    — handled in market_close/position review (config-driven)
"""
from __future__ import annotations

import datetime
import logging

import pytz

from core import config
from core.fmp import get_daily_bars, get_market_breadth

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
