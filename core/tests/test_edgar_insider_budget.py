"""Unit tests for core.edgar.get_insider_transactions — parallel + time-boxed
filing fetch.

Regression coverage for the 2026-08-06 incident: a fully serial fetch of
200+ SEC filings routinely took 10+ minutes, blowing past the worker's
540s subprocess timeout before market_open ever finished scoring — the
routine was killed mid-fetch, never marked "ran", and every catch-up retry
re-did the whole slow fetch from scratch until the 2h catch-up cap gave up
and no BUY orders were attempted all day.

Pure-logic tests: network calls are mocked, no live sec.gov access.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from core import edgar


def _reset_caches():
    edgar._cache_transactions = None
    edgar._cache_cik_map = {"0000000001": "AAPL", "0000000002": "MSFT"}


def _fake_rss_and_cik(monkeypatch, urls):
    monkeypatch.setattr(edgar, "_load_cik_ticker_map", lambda: {})

    class _FakeResp:
        status_code = 200
        text = "<feed xmlns='http://www.w3.org/2005/Atom'></feed>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(edgar.requests, "get", lambda *a, **k: _FakeResp())
    monkeypatch.setattr(edgar, "_cik_from_url", lambda url: url)  # 1 CIK per fake URL

    # Bypass the atom-feed parse entirely and hand back our fake filing URLs.
    def _fake_get_urls():
        return list(urls)

    return _fake_get_urls


def test_fetches_all_filings_when_within_budget(monkeypatch):
    """Normal case: fetch phase finishes comfortably inside the budget and
    every filing is included — no regression on the happy path."""
    _reset_caches()
    urls = [f"https://sec.gov/Archives/{i}/f4-index.htm" for i in range(20)]

    monkeypatch.setattr(edgar, "_load_cik_ticker_map", lambda: {})
    monkeypatch.setattr(edgar, "_cik_from_url", lambda url: url)
    monkeypatch.setattr(edgar, "_fetch_and_parse",
                         lambda url, ticker: [{"symbol": "TST", "transactionType": "P-Purchase"}])

    class _FakeFeedResp:
        status_code = 200
        text = "<feed xmlns='http://www.w3.org/2005/Atom'></feed>"

        def raise_for_status(self):
            pass

    with patch.object(edgar.requests, "get", return_value=_FakeFeedResp()):
        with patch.object(edgar.ET, "fromstring") as fake_parse:
            # Build a minimal fake atom feed with one <entry><link href=...>
            # per URL so the RSS-parsing loop yields our filing_urls.
            import xml.etree.ElementTree as RealET
            ns = "http://www.w3.org/2005/Atom"
            root = RealET.Element(f"{{{ns}}}feed")
            for u in urls:
                entry = RealET.SubElement(root, f"{{{ns}}}entry")
                link = RealET.SubElement(entry, f"{{{ns}}}link")
                link.set("href", u)
            fake_parse.return_value = root
            transactions = edgar.get_insider_transactions(days_lookback=30)

    assert len(transactions) == len(urls)


def test_stops_early_and_returns_partial_results_when_budget_exhausted(monkeypatch):
    """If the fetch phase overruns _FETCH_BUDGET_SEC (e.g. SEC is slow), the
    function must return whatever it has instead of hanging until every
    filing is fetched — this is what let the whole routine get killed by
    the caller's outer timeout with zero results."""
    _reset_caches()
    urls = [f"https://sec.gov/Archives/{i}/f4-index.htm" for i in range(50)]

    monkeypatch.setattr(edgar, "_load_cik_ticker_map", lambda: {})
    monkeypatch.setattr(edgar, "_cik_from_url", lambda url: url)
    monkeypatch.setattr(edgar, "_MAX_WORKERS", 2)
    monkeypatch.setattr(edgar, "_FETCH_BUDGET_SEC", 0.2)

    def _slow_fetch(url, ticker):
        time.sleep(0.3)  # slower than the whole budget
        return [{"symbol": "TST", "transactionType": "P-Purchase"}]

    monkeypatch.setattr(edgar, "_fetch_and_parse", _slow_fetch)

    class _FakeFeedResp:
        status_code = 200
        text = "<feed xmlns='http://www.w3.org/2005/Atom'></feed>"

        def raise_for_status(self):
            pass

    import xml.etree.ElementTree as RealET
    ns = "http://www.w3.org/2005/Atom"
    root = RealET.Element(f"{{{ns}}}feed")
    for u in urls:
        entry = RealET.SubElement(root, f"{{{ns}}}entry")
        link = RealET.SubElement(entry, f"{{{ns}}}link")
        link.set("href", u)

    start = time.monotonic()
    with patch.object(edgar.requests, "get", return_value=_FakeFeedResp()):
        with patch.object(edgar.ET, "fromstring", return_value=root):
            transactions = edgar.get_insider_transactions(days_lookback=30)
    elapsed = time.monotonic() - start

    # Must return well before all 50 filings could have been fetched
    # serially (50 * 0.3s = 15s) — bounded by budget + in-flight workers.
    assert elapsed < 5.0
    assert len(transactions) < len(urls)
