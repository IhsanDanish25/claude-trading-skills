"""yfinance helpers shared across screeners."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

import yfinance as yf

# Batch downloads of the full ~100-symbol universe legitimately take 35-45s.
# The old 30s cap killed every batch mid-flight → "Got bars for 0 symbols" →
# 0 candidates every run. 90s still fails fast on a real DNS outage but lets a
# normal full-universe batch complete. Override via YF_TIMEOUT env var.
_YF_TIMEOUT = int(os.environ.get("YF_TIMEOUT", "90"))  # seconds


def yf_download(*args, **kwargs):
    """yf.download() with a hard timeout via a background thread (_YF_TIMEOUT)."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(yf.download, *args, **kwargs)
        return fut.result(timeout=_YF_TIMEOUT)
