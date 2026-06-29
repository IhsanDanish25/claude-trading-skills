"""
PEAD position tracker — stores entry dates + metadata for time-based exits.

File: state/pead_positions.json
Format: {symbol: {entry_date, entry_price, surprise_pct, hold_days}}
"""
from __future__ import annotations

import json
import os
import datetime
import logging

from core.config import STATE_DIR, PEAD_HOLD_DAYS

log = logging.getLogger(__name__)
PEAD_FILE = os.path.join(STATE_DIR, "pead_positions.json")


def _load() -> dict:
    try:
        with open(PEAD_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PEAD_FILE, "w") as f:
        json.dump(data, f, indent=2)


def add_position(symbol: str, entry_price: float,
                 surprise_pct: float, report_date: str) -> None:
    data = _load()
    data[symbol] = {
        "entry_date": datetime.date.today().isoformat(),
        "entry_price": entry_price,
        "surprise_pct": surprise_pct,
        "report_date": report_date,
        "hold_days": PEAD_HOLD_DAYS,
    }
    _save(data)
    log.info(f"PEAD tracked: {symbol} surprise={surprise_pct:.1f}% hold={PEAD_HOLD_DAYS}d")


def remove_position(symbol: str) -> None:
    data = _load()
    if symbol in data:
        del data[symbol]
        _save(data)
        log.info(f"PEAD untracked: {symbol}")


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
                "symbol": sym,
                "entry_date": info["entry_date"],
                "entry_price": info["entry_price"],
                "age_days": age,
                "hold_days": hold,
            })
    return expired


def get_all() -> dict:
    return _load()


def position_age(symbol: str, today: datetime.date | None = None) -> int | None:
    today = today or datetime.date.today()
    data = _load()
    info = data.get(symbol)
    if not info:
        return None
    entry = datetime.date.fromisoformat(info["entry_date"])
    return (today - entry).days
