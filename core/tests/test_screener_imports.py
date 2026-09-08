"""Import + smoke tests for the strategy screeners wired into market_open.

These exist because both of the following shipped broken in the same commit
and were only caught by manually reading Railway logs:

  1. All 5 screeners did `from core.fmp import _get, _stable` — but
     core.fmp only exports `_STABLE` (uppercase). The ImportError was
     swallowed by a bare `except Exception: screen_x = None` in
     routines/market_open.py, so every runner silently logged
     "screener unavailable (FMP?)" and skipped instead of trading.
  2. 4 of the 5 screeners referenced `S&P80_UNIVERSE` (a stray `&` from an
     "S&P 500" find/replace) instead of `SP80_UNIVERSE`. That parses fine
     (as `S & P80_UNIVERSE`, two undefined names) but raises NameError the
     moment screen() actually runs — never on import.

Neither failure mode was network-visible: the fix must be verifiable
without live FMP credentials, hence mocking core.fmp._get.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest

import core.fmp as fmp

SCREENER_MODULES = [
    "core.meanrev_screener",
    "core.insider_screener",
    "core.squeeze_screener",
    "core.breakout_screener",
    "core.earnings_momentum_screener",
]


@pytest.mark.parametrize("module_name", SCREENER_MODULES)
def test_screener_module_imports(module_name):
    mod = importlib.import_module(module_name)
    assert callable(mod.screen)


@pytest.mark.parametrize("module_name", SCREENER_MODULES)
def test_screener_runs_without_nameerror(module_name):
    """screen() must not crash with NameError/AttributeError when FMP
    returns no data — regression guard for the S&P80_UNIVERSE typo."""
    with patch.object(fmp, "_get", return_value=[]):
        mod = importlib.reload(importlib.import_module(module_name))
        result = mod.screen()
    assert isinstance(result, list)
