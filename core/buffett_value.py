"""
Buffett Value — live-engine bridge to the skill's 4-agent scripts.

The skill's real implementation lives in skills/buffett-value/scripts/
(analyst.py, buy.py, hold.py, sell.py) so it stays a single source of
truth, testable and usable both from Claude Code and from the live
trading engine. Every other strategy in this repo has its screener as a
core/*.py module (e.g. core/breakout_screener.py) — duplicating ~1,600
lines of already-tested logic into core/ would only create a second copy
that can drift out of sync, so this module instead makes the scripts
directory importable and re-exports the functions market_open.py and
market_close.py need.

Dockerfile.worker does `COPY . .`, so skills/ ships in the live
container; this import is safe in production, not just locally.
"""
from __future__ import annotations

import os
import sys

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "buffett-value", "scripts",
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from analyst import screen_for_buffett_candidates, analyze_symbol  # noqa: E402
from buy import get_top_buy_signals  # noqa: E402
from hold import monitor_position, monitor_portfolio  # noqa: E402
from sell import evaluate_exit  # noqa: E402

__all__ = [
    "screen_for_buffett_candidates",
    "analyze_symbol",
    "get_top_buy_signals",
    "monitor_position",
    "monitor_portfolio",
    "evaluate_exit",
]
