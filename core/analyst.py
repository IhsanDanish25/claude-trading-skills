"""
Claude AI analyst.
Sends market data → gets structured BUY/SKIP/SELL decisions.
"""
from __future__ import annotations
import os
import anthropic
import json
import logging
from core.config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)
_client: anthropic.Anthropic | None = None

# Fallback chain — env var ANTHROPIC_MODELS (comma-separated) overrides.
# Default is empty so the live loop never burns Anthropic spend on hardcoded
# model IDs that may be stale; if env var is unset and a caller invokes the
# analyst, we try once and degrade gracefully.
_DEFAULT_MODELS = os.environ.get("ANTHROPIC_MODELS", "claude-opus-4-8,claude-sonnet-5").strip()
_MODELS = [m.strip() for m in _DEFAULT_MODELS.split(",") if m.strip()]


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _ask(system: str, user: str, max_tokens: int = 1024) -> str:
    import time as _time
    if not _MODELS:
        raise RuntimeError(
            "ANTHROPIC_MODELS env var not set (comma-separated model IDs). "
            "Set it in Railway Variables, e.g. ANTHROPIC_MODELS=claude-sonnet-4-20250514"
        )
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Set it in Railway Variables. "
            "pre_market VCP scoring requires a valid Anthropic API key."
        )
    client = _get_client()
    for model in _MODELS:
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            for block in msg.content:
                if block.type == "text":
                    return block.text
            raise RuntimeError("Claude returned no text block in response")
        except anthropic.NotFoundError:
            log.warning("Model %s not available, trying next", model)
            continue
        except anthropic.RateLimitError:
            log.warning("Rate limited on %s — sleeping 10s then trying next model", model)
            _time.sleep(10)
            continue
        except anthropic.AuthenticationError:
            log.error("Anthropic API key invalid (401)")
            raise
    raise RuntimeError(f"No available model — tried {_MODELS}")


def _data_driven_regime(breadth_data: dict) -> dict:
    """Deterministic regime fallback from raw market data — no AI needed."""
    spy = breadth_data.get("spy_change_pct", 0)
    qqq = breadth_data.get("qqq_change_pct", 0)
    avg = (spy + qqq) / 2

    if avg > 0.5:
        regime, bias = "bull", "aggressive"
    elif avg > -0.3:
        regime, bias = "neutral", "moderate"
    elif avg > -1.0:
        regime, bias = "bear", "defensive"
    else:
        regime, bias = "bear", "cash"

    return {
        "regime": regime,
        "confidence": 60,
        "rationale": f"data-driven: SPY {spy:+.2f}% QQQ {qqq:+.2f}%",
        "trade_bias": bias,
    }


def analyze_market_regime(breadth_data: dict) -> dict:
    """
    Input: {advancing, declining, new_highs, new_lows, spy_trend, qqq_trend}
    Output: {regime: 'bull'|'bear'|'neutral', confidence: 0-100, rationale: str, trade_bias: str}
    Falls back to deterministic regime if Claude API fails.
    """
    system = (
        "You are a systematic market regime classifier. "
        "Respond ONLY with valid JSON. No markdown, no explanation outside JSON. "
        'Schema: {"regime":"bull|bear|neutral","confidence":0-100,'
        '"rationale":"<1 sentence>","trade_bias":"aggressive|moderate|defensive|cash"}'
    )

    user = "Market breadth data: " + json.dumps(breadth_data)

    try:
        raw = _ask(system, user)
        result = json.loads(raw)
    except Exception as e:
        log.error("Regime AI call/parse fail: %s — using data-driven fallback", e)
        return _data_driven_regime(breadth_data)

    if result.get("trade_bias") == "cash":
        spy = breadth_data.get("spy_change_pct", 0)
        qqq = breadth_data.get("qqq_change_pct", 0)
        if (spy + qqq) / 2 > -0.5:
            log.warning("Claude said 'cash' but indices are flat/up — overriding to 'defensive'")
            result["trade_bias"] = "defensive"
            result["rationale"] += " [overridden: indices not bearish enough for cash]"

    return result


def score_vcp_candidates(candidates: list[dict]) -> list[dict]:
    """
    Input: list of {symbol, price, rel_volume, adr_pct, contraction_weeks,
                    tight_closes, near_52w_high}
    Output: list of {symbol, score:0-100, action:'BUY'|'WATCH'|'SKIP', reason: str}
    Sorted by score desc.
    """
    system = (
        "You are a VCP (Volatility Contraction Pattern) momentum trader. "
        "Score each candidate 0-100 for swing trade entry quality. "
        "Respond ONLY with valid JSON array. Schema per element: "
        '{"symbol":"","score":0-100,"action":"BUY|WATCH|SKIP","reason":"<1 sentence>"} '
        "Sort by score descending."
    )

    user = "VCP candidates:\n" + json.dumps(candidates, indent=2)
    raw  = _ask(system, user, max_tokens=2048)

    try:
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(clean)
    except Exception:
        log.error("VCP score parse fail: %s", raw[:300])
        return []


def review_open_positions(positions: list[dict], market_regime: str) -> list[dict]:
    """
    Input: positions = [{symbol, entry_price, current_price, pnl_pct,
                         days_held, stop, target}]
    Output: [{symbol, action:'HOLD'|'SELL'|'TIGHTEN_STOP', reason:str}]
    """
    system = (
        "You are a position management expert for swing trades. "
        "For each position decide: HOLD, SELL (exit now), or TIGHTEN_STOP (move stop up). "
        "Respond ONLY with valid JSON array. Schema per element: "
        '{"symbol":"","action":"HOLD|SELL|TIGHTEN_STOP","reason":"<1 sentence>","new_stop":null}'
    )

    user = "Market regime: " + market_regime + "\nPositions:\n" + json.dumps(positions, indent=2)
    raw  = _ask(system, user, max_tokens=1024)

    try:
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(clean)
    except Exception:
        log.error("Position review parse fail: %s", raw[:300])
        return []


def generate_weekly_summary(stats: dict) -> str:
    """
    Input: {week_pnl_pct, trades_taken, win_rate, best_trade, worst_trade,
            regime_changes, lessons: [str]}
    Output: plain text weekly summary + next week plan
    """
    system = (
        "You are a professional trading journal writer. "
        "Write a concise weekly review (max 300 words): "
        "1. Performance summary "
        "2. What worked / what did not "
        "3. Next week plan (max 3 action items) "
        "Be specific, data-driven, no fluff."
    )

    user = "Week stats:\n" + json.dumps(stats, indent=2)
    return _ask(system, user, max_tokens=600)


def detect_ftd(price_data: list[dict]) -> dict:
    """
    Input: recent daily bars [{date, open, high, low, close, volume}] last 20 days
    Output: {ftd_detected: bool, ftd_date: str|null, confidence: 0-100, details: str}
    """
    system = (
        "You are an IBD Follow-Through Day (FTD) detector. "
        "FTD = 4th+ day of rally attempt, index up 1.7%+ on higher volume than prior day. "
        "Respond ONLY with valid JSON: "
        '{"ftd_detected":true/false,"ftd_date":"YYYY-MM-DD or null",'
        '"confidence":0-100,"details":"<1 sentence>"}'
    )

    user = "Price/volume data (last 20 days):\n" + json.dumps(price_data, indent=2)
    raw  = _ask(system, user)

    try:
        return json.loads(raw)
    except Exception:
        return {
            "ftd_detected": False,
            "ftd_date": None,
            "confidence": 0,
            "details": "parse error",
        }


def _parse_json(raw: str) -> dict | list:
    clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(clean)


def build_situation_report(event_data: dict) -> dict:
    """
    Synthesize today's macro context into a situation report.
    Input: {date, breadth, high_impact_events_today, sector_rotation}
    Output: {macro_risk, trade_bias_override, event_blocks, summary}
    """
    system = (
        "You are a market intelligence analyst for a swing-trade bot. "
        "Given today's macro data, assess the trading environment. "
        "Respond ONLY with valid JSON. Schema: "
        '{"macro_risk":"low|medium|high",'
        '"trade_bias_override":null,'
        '"event_blocks":[{"event":"","time":"","impact":""}],'
        '"summary":"<2 sentences: what is happening today and how it affects swing trades>"} '
        "trade_bias_override: set to 'cash' ONLY when Fed rate decision, CPI release, "
        "or systemic crisis falls TODAY. Otherwise always null. "
        "event_blocks: list only events happening TODAY that could cause extreme intraday moves."
    )
    user = "Today's market data:\n" + json.dumps(event_data, indent=2)
    try:
        raw = _ask(system, user, max_tokens=512)
        return _parse_json(raw)
    except Exception as e:
        log.error("Situation report failed: %s", e)
        return {
            "macro_risk": "medium",
            "trade_bias_override": None,
            "event_blocks": [],
            "summary": "Research unavailable — proceeding with defaults.",
        }


def check_stock_news_batch(stock_news: dict) -> dict:
    """
    Evaluate recent headlines for each stock and flag dangerous ones.
    Input: {symbol: [headline1, headline2, ...]}
    Output: {symbol: {sentiment, risk, skip, reason}}
    """
    if not stock_news:
        return {}
    system = (
        "You are a pre-trade news risk screener for a swing-trade bot. "
        "For each stock, evaluate the recent headlines provided. "
        "Respond ONLY with a valid JSON object. One key per symbol. Schema per symbol: "
        '{"sentiment":"positive|neutral|negative","risk":"low|medium|high",'
        '"skip":false,"reason":"<1 sentence>"} '
        "skip=true ONLY for: earnings miss, fraud/SEC investigation, delisting notice, "
        "massive guidance cut, clinical trial failure, or catastrophic news. "
        "Normal volatility, analyst upgrades/downgrades, sector news = skip=false. "
        "Be conservative — only block on clearly company-destroying events."
    )
    user = "Stock headlines to evaluate:\n" + json.dumps(stock_news, indent=2)
    try:
        raw = _ask(system, user, max_tokens=1024)
        return _parse_json(raw)
    except Exception as e:
        log.error("Stock news batch check failed: %s", e)
        return {}
