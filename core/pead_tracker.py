"""
PEAD position tracker — stores entry dates + metadata for time-based exits.

File: state/pead_positions.json
Format: {symbol: {entry_date, entry_price, surprise_pct, hold_days, strategy}}
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
import threading

from core.config import (
    STATE_DIR, PEAD_HOLD_DAYS,
    MEANREV_HOLD_DAYS, INSIDER_HOLD_DAYS,
    SQUEEZE_HOLD_DAYS, BREAKOUT_HOLD_DAYS, EARNMOM_HOLD_DAYS,
)

log = logging.getLogger(__name__)
PEAD_FILE = os.path.join(STATE_DIR, "pead_positions.json")
_pead_lock = threading.Lock()

# ── Strategy defaults ─────────────────────────────────────────────────────────
# Source of truth is config.py; values here match those defaults.
_STRATEGY_HOLD_DAYS = {
    "pead":     PEAD_HOLD_DAYS,
    "meanrev":  MEANREV_HOLD_DAYS,
    "insider":  INSIDER_HOLD_DAYS,
    "squeeze":  SQUEEZE_HOLD_DAYS,
    "breakout": BREAKOUT_HOLD_DAYS,
    "earnmom":  EARNMOM_HOLD_DAYS,
}


def _load() -> dict:
    with _pead_lock:
        try:
            with open(PEAD_FILE) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as e:
            log.warning("PEAD state corrupted (%s), resetting: %s", PEAD_FILE, e)
            return {}


def _save(data: dict) -> None:
    with _pead_lock:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", dir=STATE_DIR, delete=False, suffix=".tmp"
        )
        with tmp:
            json.dump(data, tmp, indent=2)
        os.replace(tmp.name, PEAD_FILE)


def add_position(symbol: str, entry_price: float,
                 surprise_pct: float, report_date: str,
                 strategy: str = "pead",
                 hold_days: int | None = None) -> None:
    """Register a new position with optional strategy tag and custom hold_days."""
    hold_days = hold_days or _STRATEGY_HOLD_DAYS.get(strategy, PEAD_HOLD_DAYS)
    data = _load()
    data[symbol] = {
        "entry_date":    datetime.date.today().isoformat(),
        "entry_price":   entry_price,
        "surprise_pct":  surprise_pct,
        "report_date":   report_date,
        "hold_days":     hold_days,
        "strategy":      strategy,
    }
    _save(data)
    log.info(f"Position tracked: {symbol} strategy={strategy} hold={hold_days}d "
             f"surprise={surprise_pct:.1f}%")


def remove_position(symbol: str) -> None:
    data = _load()
    if symbol in data:
        del data[symbol]
        _save(data)
        log.info(f"Position untracked: {symbol}")


def update_position(symbol: str, **kwargs) -> None:
    """Update arbitrary fields on an existing position entry."""
    data = _load()
    if symbol not in data:
        log.warning("update_position: %s not in tracker", symbol)
        return
    data[symbol].update(kwargs)
    _save(data)


def get_expired(today: datetime.date | None = None) -> list[dict]:
    """Return positions that have exceeded their hold period."""
    today = today or datetime.date.today()
    data = _load()
    expired = []
    for sym, info in data.items():
        entry = datetime.date.fromisoformat(info["entry_date"])
        age = (today - entry).days
        hold = info.get("hold_days", PEAD_HOLD_DAYS)
        if age >= hold:
            expired.append({
                "symbol":      sym,
                "entry_date":  info["entry_date"],
                "entry_price": info["entry_price"],
                "age_days":    age,
                "hold_days":   hold,
                "strategy":    info.get("strategy", "pead"),
            })
    return expired


def get_all() -> dict:
    return _load()


def get_by_symbol(symbol: str) -> dict | None:
    data = _load()
    return data.get(symbol)


def position_age(symbol: str, today: datetime.date | None = None) -> int | None:
    today = today or datetime.date.today()
    data = _load()
    info = data.get(symbol)
    if not info:
        return None
    entry = datetime.date.fromisoformat(info["entry_date"])
    return (today - entry).days