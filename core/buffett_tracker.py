"""
Buffett Value position tracker — stores the entry-time analyst snapshot so
hold.py can re-compare fundamentals against it later.

File: state/buffett_positions.json
Format: {symbol: {entry_date, entry_price, qty, entry_snapshot}}

Deliberately has no hold-days/expiry concept and no get_expired()-style
function, unlike core/pead_tracker.py: Buffett Value never exits on a
calendar basis, only on profit target, fundamentals deterioration, or a
better opportunity (see skills/buffett-value/scripts/sell.py).
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
import threading
from typing import Any

from core.config import STATE_DIR

log = logging.getLogger(__name__)
BUFFETT_FILE = os.path.join(STATE_DIR, "buffett_positions.json")
_buffett_lock = threading.Lock()


def _load() -> dict:
    with _buffett_lock:
        try:
            with open(BUFFETT_FILE) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as e:
            log.warning("Buffett Value state corrupted (%s), resetting: %s", BUFFETT_FILE, e)
            return {}


def _save(data: dict) -> None:
    with _buffett_lock:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", dir=STATE_DIR, delete=False, suffix=".tmp"
        )
        with tmp:
            json.dump(data, tmp, indent=2)
        os.replace(tmp.name, BUFFETT_FILE)


def add_position(symbol: str, entry_price: float, qty: float,
                  entry_snapshot: dict[str, Any]) -> None:
    """Register a new position with its full analyst-agent candidate dict."""
    data = _load()
    data[symbol] = {
        "entry_date": datetime.date.today().isoformat(),
        "entry_price": entry_price,
        "qty": qty,
        "entry_snapshot": entry_snapshot,
        "strategy": "buffett_value",
    }
    _save(data)
    log.info(f"Buffett Value position tracked: {symbol} qty={qty} entry=${entry_price:.2f}")


def remove_position(symbol: str) -> None:
    data = _load()
    if symbol in data:
        del data[symbol]
        _save(data)
        log.info(f"Buffett Value position untracked: {symbol}")


def get_all() -> dict:
    return _load()


def get_by_symbol(symbol: str) -> dict | None:
    data = _load()
    return data.get(symbol)


def is_tracked(symbol: str) -> bool:
    return get_by_symbol(symbol) is not None
