"""Helpers for reading alpaca-py order objects."""
from __future__ import annotations


def order_field(order, field: str) -> str:
    """Normalize an alpaca order attribute to a lowercase plain string.

    alpaca-py enums are str mixins whose str() is 'OrderSide.SELL', not
    'sell', so `str(o.side).lower() == "sell"` silently never matches a
    real order. Read the enum's .value instead; raw strings pass through.
    """
    val = getattr(order, field, "") or ""
    val = getattr(val, "value", val)
    return str(val).lower()
