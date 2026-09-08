"""Regression test for the missing ET timezone constant.

core/screener.py and the routines/ entry points rely on a timezone-aware
ET constant. It was dropped from core/config.py during the alpaca-py
migration (commit 4e00f68), which cascaded into unguarded import failures
in routines/market_open.py, routines/midday_review.py, and
routines/pre_market.py. This guards against the constant disappearing
again and against any of those modules failing to import.
"""

from __future__ import annotations

import importlib

import pytz
import pytest

from core.config import ET

IMPORTABLE_MODULES = [
    "core.screener",
    "routines.market_open",
    "routines.midday_review",
    "routines.pre_market",
]


def test_et_constant_exists_and_is_new_york():
    assert ET == pytz.timezone("America/New_York")


@pytest.mark.parametrize("module_name", IMPORTABLE_MODULES)
def test_module_imports_without_error(module_name):
    importlib.import_module(module_name)
