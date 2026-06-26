from __future__ import annotations
"""
COMPOSITE SCORING ENGINE — market_open candidate ranking.

Integrates the *scoring logic* of the GROUP A trading skills (Alpaca-native /
no-API) into a single weighted 0-100 composite, plus GROUP B (FMP-dependent)
sub-scores that fail gracefully to a neutral 50 on any error (including HTTP
429), so the live loop never crashes on a rate limit.

Each sub-scorer returns a 0-100 sub-score. They are combined with fixed weights
into a composite, which is then scaled by a market-regime multiplier. The full
per-candidate breakdown is returned for logging.

Skill → sub-scorer map (see SKILLS_AUDIT report):
  GROUP A (Alpaca bars / candidate fields, no paid API):
    vcp          ← vcp-screener                       (VCP structure)
    rs           ← RS-rating / CANSLIM-L / druckenmiller leadership
    breakout     ← breakout-scanner                   (proximity to high + vol)
    trend        ← technical-analyst / us-stock-analysis (MA stack / stage-2)
    momentum     ← technical-indicator-suite          (RSI + MACD)
    volume_trend ← PEAD/earnings volume factor, breakout volume confirm
    sector       ← sector-rotation-detector / sector-analyst (ETF momentum)
    [regime is a market-level multiplier built from market-breadth-analyzer /
     uptrend-analyzer / ftd-detector / market-top-detector / exposure-coach]
  GROUP B (FMP, graceful-fail → neutral 50 on 429/error):
    earnings     ← earnings-calendar / earnings-trade-analyzer (blackout)
    fundamental  ← canslim-screener / value-dividend-screener  (cap/quality)
"""
import logging
import os

log = logging.getLogger(__name__)

# ── Weights (GROUP A + GROUP B), must sum to 1.0 ───────────────────────────────
WEIGHTS = {
    "vcp":          0.20,   # GROUP A
    "rs":           0.18,   # GROUP A
    "breakout":     0.15,   # GROUP A
    "trend":        0.12,   # GROUP A
    "momentum":     0.10,   # GROUP A
    "volume_trend": 0.08,   # GROUP A
    "sector":       0.07,   # GROUP A
    "earnings":     0.05,   # GROUP B (graceful)
    "fundamental":  0.05,   # GROUP B (graceful)
}

GROUP = {
    "vcp": "A", "rs": "A", "breakout": "A", "trend": "A", "momentum": "A",
    "volume_trend": "A", "sector": "A", "earnings": "B", "fundamental": "B",
}

NEUTRAL = 50.0  # neutral sub-score used when a signal can't be computed

USE_FMP = os.environ.get("COMPOSITE_USE_FMP", "true").lower() == "true"

# Sector ETFs ranked for the `sector` sub-score (all available on Alpaca IEX).
SECTOR_ETFS = ["XLK", "XLC", "XLY", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLB", "XLRE"]

# Static symbol→sector-ETF map for the VCP watchlist (no API needed).
SECTOR_MAP = {
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK", "CRM": "XLK",
    "ADBE": "XLK", "PANW": "XLK", "CRWD": "XLK", "SNOW": "XLK", "DDOG": "XLK",
    "NET": "XLK", "ZS": "XLK", "ON": "XLK", "AEHR": "XLK", "SMCI": "XLK", "AXON": "XLK",
    "META": "XLC", "GOOGL": "XLC", "NFLX": "XLC", "PINS": "XLC",
    "AMZN": "XLY", "TSLA": "XLY", "MELI": "XLY", "SHOP": "XLY", "SQ": "XLY", "DUOL": "XLY",
    "ENPH": "XLE", "FSLR": "XLE",
    "CELH": "XLP", "COCO": "XLP",
}


# ════════════════════════════════════════════════════════════════════════════
# Pure math helpers (operate on closes oldest→newest unless noted)
# ════════════════════════════════════════════════════════════════════════════
def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _lerp_score(x: float, lo: float, hi: float) -> float:
    """Linear map x in [lo, hi] → [0, 100], clamped."""
    if hi == lo:
        return NEUTRAL
    return _clamp((x - lo) / (hi - lo) * 100.0)


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def _ema(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    k = 2.0 / (n + 1)
    ema = sum(values[:n]) / n
    for v in values[n:]:
        ema = v * k + ema * (1 - k)
    return ema


def _ema_series(values: list[float], n: int) -> list[float]:
    if len(values) < n or n <= 0:
        return []
    k = 2.0 / (n + 1)
    ema = sum(values[:n]) / n
    out = [ema]
    for v in values[n:]:
        ema = v * k + ema * (1 - k)
        out.append(ema)
    return out


def _rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder RSI on closes oldest→newest."""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _macd_hist(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> float | None:
    """MACD histogram (latest) on closes oldest→newest."""
    if len(closes) < slow + signal:
        return None
    fast_e = _ema_series(closes, fast)
    slow_e = _ema_series(closes, slow)
    # align tails
    m = min(len(fast_e), len(slow_e))
    macd_line = [fast_e[-m + i] - slow_e[-m + i] for i in range(m)]
    sig = _ema_series(macd_line, signal)
    if not sig:
        return None
    return macd_line[-1] - sig[-1]


def _closes_oldest_first(bars: list[dict]) -> list[float]:
    """Screener bars are newest-first; return closes oldest→newest."""
    return [b["close"] for b in reversed(bars)]


def _volumes_oldest_first(bars: list[dict]) -> list[float]:
    return [b.get("volume", 0) for b in reversed(bars)]


# ════════════════════════════════════════════════════════════════════════════
# GROUP A sub-scorers
# ════════════════════════════════════════════════════════════════════════════
def score_vcp(candidate: dict) -> float:
    """vcp-screener: reuse the screener's raw VCP score (already 0-100)."""
    raw = candidate.get("raw_score", candidate.get("score"))
    if raw is None:
        return NEUTRAL
    return _clamp(float(raw))


def score_rs(candidate: dict) -> float:
    """Relative strength vs SPY (1-mo). RS -5%→0, +20%→100."""
    rs = candidate.get("rs_vs_spy")
    if rs is None:
        return NEUTRAL
    return _lerp_score(float(rs), -5.0, 20.0)


def score_breakout(candidate: dict) -> float:
    """breakout-scanner: proximity to window high (-12%→0, 0%→100) + volume surge."""
    pct = candidate.get("pct_from_52w_high")
    relv = candidate.get("rel_volume", 0) or 0
    if pct is None:
        base = NEUTRAL
    else:
        base = _lerp_score(float(pct), -12.0, 0.0)
    vol_bonus = _clamp((float(relv) - 1.0) / 1.5 * 20.0, 0.0, 20.0)  # up to +20 at 2.5x
    return _clamp(base * 0.8 + vol_bonus)


def score_trend(bars: list[dict] | None, candidate: dict) -> float:
    """technical-analyst / us-stock-analysis: Stage-2 MA stack.
    price>MA20>MA50>MA200 and price>MA200 are the stacked-uptrend tells."""
    if not bars:
        return NEUTRAL
    closes = _closes_oldest_first(bars)
    price = closes[-1] if closes else None
    if price is None:
        return NEUTRAL
    ma20, ma50, ma200 = _sma(closes, 20), _sma(closes, 50), _sma(closes, 200)
    pts, total = 0, 0
    if ma20 is not None:
        total += 1; pts += 1 if price > ma20 else 0
    if ma50 is not None:
        total += 1; pts += 1 if price > ma50 else 0
    if ma200 is not None:
        total += 1; pts += 1 if price > ma200 else 0
    if ma20 is not None and ma50 is not None:
        total += 1; pts += 1 if ma20 > ma50 else 0
    if ma50 is not None and ma200 is not None:
        total += 1; pts += 1 if ma50 > ma200 else 0
    if total == 0:
        return NEUTRAL
    return _clamp(pts / total * 100.0)


def score_momentum(bars: list[dict] | None) -> float:
    """technical-indicator-suite: RSI(14) sweet-spot 55-70 + MACD histogram > 0."""
    if not bars:
        return NEUTRAL
    closes = _closes_oldest_first(bars)
    rsi = _rsi(closes)
    hist = _macd_hist(closes)
    parts = []
    if rsi is not None:
        # peak score at RSI 62; taper toward overbought (>78) and weak (<45)
        if rsi <= 45:
            parts.append(_lerp_score(rsi, 30, 45) * 0.5)
        elif rsi <= 62:
            parts.append(_lerp_score(rsi, 45, 62))
        elif rsi <= 78:
            parts.append(100.0 - (rsi - 62) / (78 - 62) * 40.0)  # 100→60
        else:
            parts.append(_clamp(60.0 - (rsi - 78) * 4.0))        # overbought taper
    if hist is not None:
        parts.append(100.0 if hist > 0 else 30.0)
    if not parts:
        return NEUTRAL
    return _clamp(sum(parts) / len(parts))


def score_volume_trend(bars: list[dict] | None, candidate: dict) -> float:
    """PEAD / breakout volume confirm: recent 5d avg volume vs prior 20d avg
    (accumulation), blended with today's relative volume."""
    relv = candidate.get("rel_volume")
    accel = NEUTRAL
    if bars:
        vols = _volumes_oldest_first(bars)
        if len(vols) >= 25:
            recent = sum(vols[-5:]) / 5
            prior = sum(vols[-25:-5]) / 20
            if prior > 0:
                ratio = recent / prior
                accel = _lerp_score(ratio, 0.7, 1.8)
    if relv is None:
        return accel
    relv_score = _lerp_score(float(relv), 0.7, 2.0)
    return _clamp(accel * 0.5 + relv_score * 0.5)


def score_sector(candidate: dict, sector_mom: dict) -> float:
    """sector-rotation-detector: candidate's sector-ETF momentum percentile."""
    if not sector_mom:
        return NEUTRAL
    etf = SECTOR_MAP.get(candidate.get("symbol", ""))
    if etf is None or etf not in sector_mom:
        return NEUTRAL
    return _clamp(float(sector_mom[etf]))


# ════════════════════════════════════════════════════════════════════════════
# GROUP B sub-scorers (FMP — must NEVER raise; neutral 50 on any failure)
# ════════════════════════════════════════════════════════════════════════════
def score_earnings(candidate: dict, blackout_days: int = 3) -> float:
    """earnings-calendar: penalise entries inside the earnings blackout window.
    Any FMP error / 429 → neutral 50."""
    if not USE_FMP:
        return NEUTRAL
    try:
        import datetime
        from core.fmp import get_next_earnings
        nxt = get_next_earnings(candidate["symbol"])
        if not nxt:
            return 70.0  # no known earnings soon → mildly favourable
        ed = datetime.datetime.strptime(nxt, "%Y-%m-%d").date()
        days_out = (ed - datetime.date.today()).days
        if days_out < 0:
            return 70.0
        if days_out <= blackout_days:
            return 0.0          # inside blackout — strongly avoid
        if days_out <= 10:
            return _lerp_score(days_out, blackout_days, 10)  # ramp back to favourable
        return 80.0
    except Exception as e:                     # noqa: BLE001 — graceful, never crash
        log.debug("score_earnings graceful-fail %s: %s", candidate.get("symbol"), e)
        return NEUTRAL


def score_fundamental(candidate: dict) -> float:
    """canslim / value-dividend: light quality proxy via FMP 52w stats
    (market cap floor + distance from 52w high). Any FMP error / 429 → neutral 50."""
    if not USE_FMP:
        return NEUTRAL
    try:
        from core.fmp import get_52w_stats
        stats = get_52w_stats(candidate["symbol"])
        if not stats:
            return NEUTRAL
        cap = stats.get("market_cap", 0) or 0
        cap_score = _lerp_score(cap / 1e9, 0.5, 50.0)  # $0.5B→0, $50B→100
        pct_from_high = stats.get("pct_from_high", candidate.get("pct_from_52w_high", 0)) or 0
        prox_score = _lerp_score(float(pct_from_high), -25.0, 0.0)
        return _clamp(cap_score * 0.5 + prox_score * 0.5)
    except Exception as e:                     # noqa: BLE001 — graceful, never crash
        log.debug("score_fundamental graceful-fail %s: %s", candidate.get("symbol"), e)
        return NEUTRAL


# ════════════════════════════════════════════════════════════════════════════
# Market-regime context (market-breadth / uptrend / ftd / market-top / exposure)
# ════════════════════════════════════════════════════════════════════════════
def _etf_momentum(bars: list[dict] | None) -> float | None:
    """0-100 momentum score for one ETF from its daily bars (price vs MA50 +
    1-month return)."""
    if not bars:
        return None
    closes = _closes_oldest_first(bars)
    if len(closes) < 21:
        return None
    price = closes[-1]
    ma50 = _sma(closes, 50)
    ret_1m = (closes[-1] - closes[-21]) / closes[-21] * 100 if closes[-21] > 0 else 0.0
    above = 60.0 if (ma50 and price > ma50) else 30.0
    ret_score = _lerp_score(ret_1m, -8.0, 12.0)
    return _clamp(above * 0.4 + ret_score * 0.6)


def build_context(extra_symbols: list[str] | None = None) -> dict:
    """Fetch index + sector-ETF bars ONCE (Alpaca IEX) and derive:
      - regime_score (0-100) and regime_mult (0.55-1.0)
      - sector_mom: {ETF: 0-100 momentum}
    Lazy-imports the Alpaca-backed fetcher so this module stays importable
    without alpaca installed (pure scorers remain unit-testable)."""
    ctx = {"regime_score": NEUTRAL, "regime_mult": 1.0, "sector_mom": {}}
    try:
        from core.screener import _fetch_bars
    except Exception as e:                     # noqa: BLE001
        log.warning("composite context: cannot import bar fetcher (%s) — neutral regime", e)
        return ctx

    syms = ["SPY", "QQQ", "IWM"] + SECTOR_ETFS + list(extra_symbols or [])
    syms = list(dict.fromkeys(syms))
    try:
        bars_map = _fetch_bars(syms, days=60)
    except Exception as e:                     # noqa: BLE001
        log.warning("composite context: bar fetch failed (%s) — neutral regime", e)
        return ctx

    # Market regime from SPY/QQQ/IWM
    regime_parts = []
    for idx in ("SPY", "QQQ", "IWM"):
        m = _etf_momentum(bars_map.get(idx))
        if m is not None:
            regime_parts.append(m)
    if regime_parts:
        ctx["regime_score"] = round(sum(regime_parts) / len(regime_parts), 1)
    # Map regime 0-100 → multiplier 0.55-1.0 (risk-off shrinks composite, never 0)
    ctx["regime_mult"] = round(0.55 + ctx["regime_score"] / 100.0 * 0.45, 3)

    # Sector momentum
    sector_mom = {}
    for etf in SECTOR_ETFS:
        m = _etf_momentum(bars_map.get(etf))
        if m is not None:
            sector_mom[etf] = round(m, 1)
    ctx["sector_mom"] = sector_mom
    return ctx


# ════════════════════════════════════════════════════════════════════════════
# Composite assembly
# ════════════════════════════════════════════════════════════════════════════
def compute_composite(candidate: dict, bars: list[dict] | None, ctx: dict) -> dict:
    """Compute every sub-score, the weighted composite, and the regime-scaled
    final. Returns a dict with the full breakdown for logging."""
    ctx = ctx or {}
    sector_mom = ctx.get("sector_mom", {})

    subs = {
        "vcp":          score_vcp(candidate),
        "rs":           score_rs(candidate),
        "breakout":     score_breakout(candidate),
        "trend":        score_trend(bars, candidate),
        "momentum":     score_momentum(bars),
        "volume_trend": score_volume_trend(bars, candidate),
        "sector":       score_sector(candidate, sector_mom),
        "earnings":     score_earnings(candidate),
        "fundamental":  score_fundamental(candidate),
    }

    breakdown = {}
    composite = 0.0
    for name, raw in subs.items():
        w = WEIGHTS[name]
        contrib = raw * w
        composite += contrib
        breakdown[name] = {
            "raw": round(raw, 1),
            "weight": w,
            "contribution": round(contrib, 2),
            "group": GROUP[name],
        }

    regime_mult = float(ctx.get("regime_mult", 1.0))
    final = round(composite * regime_mult, 2)

    return {
        "symbol": candidate.get("symbol"),
        "composite": round(composite, 2),
        "regime_mult": regime_mult,
        "regime_score": ctx.get("regime_score", NEUTRAL),
        "final": final,
        "breakdown": breakdown,
    }


def format_breakdown(result: dict) -> str:
    """One-line-per-subscore human log block."""
    lines = [
        f"  COMPOSITE {result['symbol']}: final={result['final']} "
        f"(raw={result['composite']} × regime_mult={result['regime_mult']} "
        f"[regime={result['regime_score']}])"
    ]
    for name, d in result["breakdown"].items():
        lines.append(
            f"      [{d['group']}] {name:<13} raw={d['raw']:>5} "
            f"× w={d['weight']:.2f} → {d['contribution']:>5}"
        )
    return "\n".join(lines)
