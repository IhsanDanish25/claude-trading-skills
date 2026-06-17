#!/usr/bin/env python3
"""Test Alpaca API connection using the centralized client.

Thin wrapper around ``scripts/alpaca_client.py`` — run this to verify
that your Alpaca credentials are configured correctly.

Usage:
    python3 check_alpaca_connection.py

Environment Variables (set locally or in Railway service dashboard):
    ALPACA_API_KEY: Your Alpaca API Key ID
    ALPACA_SECRET_KEY: Your Alpaca Secret Key
    ALPACA_PAPER: 'true' for paper trading, 'false' for live (default: true)
"""

import sys
from pathlib import Path

# Make the shared scripts/ directory importable.
_scripts_dir = str(Path(__file__).resolve().parents[3] / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from alpaca_client import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
