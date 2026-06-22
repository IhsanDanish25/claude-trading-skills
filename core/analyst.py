"""
Claude AI analyst.
Sends market data → gets structured BUY/SKIP/SELL decisions.
"""
from __future__ import annotations
import anthropic
import json
import logging
from core.config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)
_client: anthropic.Anthropic | None = None

_MODELS = ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _ask(system: str, user: str, max_tokens: int = 1024) -> str:
    import time as _time
    client = _get_client()
    for model in _MODELS:
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return msg.content[0].text
        except anthropic.NotFoundError:
            log.warning("Model %s not available, trying next", model)
            continue
        except anthropic.RateLimitError:
            log.warning("Rate limited on %s — sleeping 10s then trying next model", model)
            _time.sleep(10)
            continue
        except anthropic.AuthenticationError:
            log.error("Anthropic API key invalid")
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

    # Override: if Claude says "cash" but indices are flat/up, downgrade to defensive
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
