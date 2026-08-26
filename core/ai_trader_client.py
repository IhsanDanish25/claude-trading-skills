"""
AI-Trader publisher — best-effort trade-signal sync to the ai4trade.ai social
trading platform (https://ai4trade.ai/skill/ai4trade).

Mirrors already-executed fills to the public signal feed; never places or
influences an order. Off by default — set AI_TRADER_ENABLED=true only after
registering an agent (POST /api/claw/agents/selfRegister) and storing the
resulting token.

Env vars (set in Railway secrets):
    AI_TRADER_ENABLED   "true" to publish live fills (default: "false" — no-op)
    AI_TRADER_TOKEN      Bearer token from agent registration
    AI_TRADER_BASE_URL   Platform base URL (default: https://ai4trade.ai)
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

_ENABLED = os.environ.get("AI_TRADER_ENABLED", "false").lower() == "true"
_TOKEN = os.environ.get("AI_TRADER_TOKEN", "")
_BASE_URL = os.environ.get("AI_TRADER_BASE_URL", "https://ai4trade.ai")

_VALID_ACTIONS = {"buy", "sell", "short", "cover"}


def publish_trade(entry: dict) -> bool:
    """Mirror one already-executed fill to AI-Trader as a realtime signal.

    entry is the same dict market_open._append_trade_log receives: symbol,
    side, qty, price, strategy, ts (ISO 8601). Silent no-op if disabled,
    unconfigured, or the entry is missing required fields. Never raises —
    a publish failure must not affect the trading path.
    """
    if not _ENABLED or not _TOKEN:
        return False

    action = entry.get("side")
    symbol = entry.get("symbol")
    price = entry.get("price")
    qty = entry.get("qty")
    executed_at = entry.get("ts")
    if action not in _VALID_ACTIONS or not symbol or price is None or qty is None or not executed_at:
        log.debug("AI-Trader publish skipped — incomplete entry: %s", entry)
        return False

    strategy = entry.get("strategy", "")
    market = "crypto" if strategy == "crypto" else "us-stock"
    payload = {
        "market": market,
        "action": action,
        "symbol": symbol,
        "price": price,
        "quantity": qty,
        "content": f"{strategy} strategy".strip(),
        "executed_at": executed_at,
    }
    try:
        resp = requests.post(
            f"{_BASE_URL}/api/signals/realtime",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            json=payload,
            timeout=10,
        )
        if resp.status_code >= 400:
            log.warning("AI-Trader publish failed (HTTP %s): %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as e:
        log.warning("AI-Trader publish failed (non-blocking): %s", e)
        return False
