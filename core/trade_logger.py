"""
Durable trade-decision logging — Axiom (primary) + trade_log.jsonl (local fallback).

Every decision in the trade lifecycle — signal detected, risk gate passed/failed,
order placed/skipped — is recorded to BOTH sinks so the full forensic trail
survives Railway redeploys, which wipe the ephemeral filesystem (see
docs/dev/strategy-validation-status.md and the July-2 logs that were lost to a
REMOVED deployment).

Axiom is best-effort: if AXIOM_API_TOKEN is unset, the SDK is missing, or an
ingest call fails, we degrade silently to the local JSONL file. Logging must
NEVER raise into the trading path — a broken log sink cannot be allowed to block
or crash an order.

Env vars (set in Railway secrets):
  AXIOM_API_TOKEN  — API token (xaat-...). Required to enable Axiom; unset = jsonl-only.
  AXIOM_DATASET    — target dataset name (default: "trade-decisions").
  AXIOM_ORG_ID     — org id; only needed for personal tokens (xapt-...), optional for xaat-.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading

import pytz

from core.config import STATE_DIR

log = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

TRADE_LOG_PATH = os.path.join(STATE_DIR, "trade_log.jsonl")
_write_lock = threading.Lock()

AXIOM_API_TOKEN = os.environ.get("AXIOM_API_TOKEN", "")
AXIOM_DATASET   = os.environ.get("AXIOM_DATASET", "trade-decisions")
AXIOM_ORG_ID    = os.environ.get("AXIOM_ORG_ID", "")

_axiom_client = None
_axiom_init_done = False


def _get_axiom():
    """Lazily construct the Axiom client once. Returns None if unconfigured or
    unavailable — callers treat None as 'Axiom disabled, use jsonl only'."""
    global _axiom_client, _axiom_init_done
    if _axiom_init_done:
        return _axiom_client
    _axiom_init_done = True
    if not AXIOM_API_TOKEN:
        log.info("Axiom logging disabled — AXIOM_API_TOKEN not set (jsonl-only)")
        return None
    try:
        from axiom_py import Client
        _axiom_client = Client(token=AXIOM_API_TOKEN, org_id=AXIOM_ORG_ID or None)
        log.info("Axiom logging enabled — dataset=%s", AXIOM_DATASET)
    except Exception as e:
        log.warning("Axiom client init failed (%s) — falling back to jsonl-only", e)
        _axiom_client = None
    return _axiom_client


def _append_jsonl(record: dict) -> None:
    """Local fallback sink — one JSONL line to state/trade_log.jsonl."""
    with _write_lock:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(TRADE_LOG_PATH, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            log.warning("trade_log append failed: %s", e)


def _send_axiom(record: dict) -> None:
    """Best-effort Axiom ingest. Never raises."""
    client = _get_axiom()
    if client is None:
        return
    try:
        client.ingest_events(dataset=AXIOM_DATASET, events=[record])
    except Exception as e:
        log.warning("Axiom ingest failed (non-blocking): %s", e)


def append_record(record: dict) -> None:
    """Write a fully-formed record to BOTH sinks (jsonl fallback + Axiom).

    Used for the buy/order record, which must keep its `side`/`pnl_pct` fields so
    market_open._reconcile_closed_trades can still match it. Extra `event` field
    is harmless to reconcile (it filters on side == "buy")."""
    _append_jsonl(record)
    _send_axiom(record)


def log_event(event_type: str, strategy: str, symbol: str | None = None, **fields) -> dict:
    """Record one trade-lifecycle decision to Axiom + trade_log.jsonl.

    event_type: signal_detected | gate_passed | gate_failed | order_placed | order_skipped
    strategy:   pead | meanrev | insider | squeeze | breakout | earnmom
    Extra keyword fields (surprise_pct, gate, reason, amount, ...) are recorded
    verbatim. Returns the enriched event dict. Never raises."""
    record = {
        "ts": datetime.datetime.now(ET).isoformat(timespec="seconds"),
        "event": event_type,
        "strategy": strategy,
        "symbol": symbol,
        **fields,
    }
    append_record(record)
    return record
