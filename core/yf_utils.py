"""yfinance helpers shared across screeners."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

import yfinance as yf

_YF_TIMEOUT = 30  # seconds — prevents hangs when Yahoo DNS is unreachable


def yf_download(*args, **kwargs):
    """yf.download() with a hard 30-second timeout via a background thread."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(yf.download, *args, **kwargs)
        return fut.result(timeout=_YF_TIMEOUT)
