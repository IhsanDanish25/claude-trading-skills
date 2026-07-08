"""
SEC EDGAR Form 4 insider transactions — zero API key required.

Uses SEC's public EDGAR API:
  1. RSS feed: recent Form 4 filings (max 200 per query)
  2. Per-filing XML: direct descendant extraction + recurse for <value>-nested fields
  3. Company tickers JSON: CIK → ticker resolution

SEC rate limit: 10 req/s — enforced with 0.15s delay between calls.

Returns a list of transaction dicts matching the FMP shape:
  {symbol, transactionDate, transactionType, securitiesOwned, sharesAmount,
   price, finalAmount, isDirector, name}

In-process cached — repeated calls in the same run cost 0 HTTP requests.
Only P (Purchase) transactions are returned to match the FMP insider signal.
"""
from __future__ import annotations

import datetime
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Iterator

import requests

from core.config import INSIDER_LOOKBACK_DAYS

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "TradingBot research@email.com",
    "Accept-Encoding": "gzip, deflate",
}

_RSS_URL     = "https://www.sec.gov/cgi-bin/browse-edgar"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_RSS_COUNT   = 200

# In-process caches
_cache_transactions: list[dict] | None = None
_cache_cik_map: dict[str, str] | None = None


# ── Tag utilities ──────────────────────────────────────────────────────────────
def _ltag(elem: ET.Element) -> str:
    """Local tag name without namespace prefix."""
    return elem.tag.split("}")[-1].lstrip("{")


def _strip(elem: ET.Element) -> str:
    return (elem.text or "").strip()


def _float(s: str) -> float:
    try:
        return float(s.replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _cik_from_url(url: str) -> str | None:
    m = re.search(r"/data/(\d+)/", url)
    if not m:
        return None
    cik = m.group(1).lstrip("0") or "0"
    return cik.zfill(10)


def _index_to_xml_url(index_url: str) -> str:
    """
    Convert a -index.htm URL to the primarydocument.xml URL.

    Index:  .../CIK/ACC_NO/ACC_NO-COUNT-index.htm
    XML:    .../CIK/ACC_NO/primarydocument.xml
    """
    # Strip trailing components to get the submission directory
    # e.g. https://www.sec.gov/Archives/edgar/data/1050377/000105037726000006/000...-index.htm
    #      → https://www.sec.gov/Archives/edgar/data/1050377/000105037726000006/primarydocument.xml
    parts = index_url.rsplit("/", 1)
    if len(parts) == 2:
        return parts[0] + "/primarydocument.xml"
    return index_url  # fallback


# ── Transaction extraction ─────────────────────────────────────────────────────
_SKIP_SECURITIES = {"preferred", "warrant", "option", "unit", "right",
                    "depositary share", "share unit"}


def _inner_value(elem: ET.Element) -> str:
    """Get text from elem, recursing into <value> child if direct text is empty."""
    v = _strip(elem)
    if v:
        return v
    for child in elem:
        if _ltag(child) == "value":
            return _strip(child)
    return ""


def _collect_fields(block: ET.Element) -> dict[str, str]:
    """Recursively collect named fields from a transaction block.

    Handles both flat <field><value>x</value></field> and
    nested <field><value><subfield>...</subfield></value></field> patterns.
    """
    result: dict[str, str] = {}

    def _recurse(parent: ET.Element):
        for child in parent:
            tag = _ltag(child)
            text = _inner_value(child)
            if text:
                result[tag] = text
            else:
                _recurse(child)

    _recurse(block)
    return result


def _walk_transactions(root: ET.Element) -> Iterator[dict]:
    """Walk the XML tree and yield one dict per transaction block."""
    TXN_TAGS = {"nonDerivativeTransaction", "derivativeTransaction"}

    def _iter_elem(elem: ET.Element):
        if _ltag(elem) in TXN_TAGS:
            yield _collect_fields(elem)
        else:
            for child in elem:
                yield from _iter_elem(child)

    yield from _iter_elem(root)


# ── Ticker map ─────────────────────────────────────────────────────────────────
def _load_cik_ticker_map() -> dict[str, str]:
    global _cache_cik_map
    if _cache_cik_map is not None:
        return _cache_cik_map

    log.info("EDGAR: loading company_tickers.json (CIK→ticker map)")
    try:
        r = requests.get(_TICKERS_URL, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        raw = r.json()
        ticker_map: dict[str, str] = {}
        # Format: {"0": {"cik_str": 1045810, "ticker": "NVDA", ...}, ...}
        for entry in raw.values():
            if isinstance(entry, dict) and entry.get("ticker"):
                cik = str(entry["cik_str"]).zfill(10)
                ticker = str(entry["ticker"]).strip()
                if ticker and cik:
                    ticker_map[cik] = ticker
        _cache_cik_map = ticker_map
        log.info("  Loaded %d CIK→ticker mappings", len(ticker_map))
        return ticker_map
    except Exception as e:
        log.warning("EDGAR: company_tickers.json failed: %s", e)
        _cache_cik_map = {}
        return {}


# ── Single-filing parser ──────────────────────────────────────────────────────
def _parse_filing(xml_text: str) -> tuple[dict, list[dict]]:
    """
    Parse one Form 4 XML and return (issuer_info, transaction_dicts).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}, []

    issuer: dict = {"symbol": "", "isDirector": False,
                     "name": "", "officerTitle": ""}

    # Extract issuer ticker
    for elem in root.iter():
        if _ltag(elem) == "issuerTradingSymbol":
            issuer["symbol"] = _inner_value(elem)
            break

    # Extract owner name and director flag
    for block in root.iter():
        if _ltag(block) == "reportingOwnerRelationship":
            if _inner_value(block.find("isDirector") or block).lower() == "true":
                issuer["isDirector"] = True
            t = _inner_value(block.find("officerTitle") or block)
            if t:
                issuer["officerTitle"] = t

    for block in root.iter():
        if _ltag(block) == "reportingOwnerId":
            name = _inner_value(block.find("rptOwnerName") or block)
            if name:
                issuer["name"] = name

    # Collect all transaction blocks
    transactions: list[dict] = []
    for txn in _walk_transactions(root):
        code = txn.get("transactionCode", "")
        if not code or code != "P":          # Only P = Purchase
            continue

        sec_title = txn.get("securityTitle", "")
        if sec_title:
            skip = any(kw in sec_title.lower() for kw in _SKIP_SECURITIES)
            if skip:
                continue

        shares = _float(txn.get("transactionShares", ""))
        price  = _float(txn.get("transactionPricePerShare", ""))
        amount = shares * price if shares and price else 0.0

        # transactionDate may be missing — fall back to periodOfReport
        txn_date = txn.get("transactionDate", "")
        if not txn_date:
            for elem in root.iter():
                if _ltag(elem) == "periodOfReport":
                    txn_date = _inner_value(elem)
                    break

        shares_str = txn.get("transactionShares", "0")
        price_str  = txn.get("transactionPricePerShare", "0")

        transactions.append({
            "securityTitle":     sec_title,
            "transactionCode":   code,
            "transactionDate":   txn_date[:10] if txn_date else "",
            "transactionShares": shares_str,
            "price":             price_str,
            "finalAmount":       amount,
        })

    return issuer, transactions


def _fetch_and_parse(url: str, ticker_fallback: str) -> list[dict]:
    """Fetch one Form 4 XML, parse, return FMP-shaped dicts. 0 HTTP on error."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        if r.status_code == 429:
            log.warning("EDGAR rate-limited (429): %s", url)
            return []
        r.raise_for_status()
        xml_text = r.text
    except Exception as e:
        log.debug("EDGAR fetch failed %s: %s", url, e)
        return []

    issuer_info, txns = _parse_filing(xml_text)

    # Use ticker from XML (most reliable); fall back to CIK→ticker map
    symbol = issuer_info.get("symbol") or ticker_fallback
    if not symbol:
        return []

    results: list[dict] = []
    for t in txns:
        results.append({
            "symbol":           symbol,
            "transactionDate":  t["transactionDate"],
            "transactionType":  "P-Purchase",
            "securitiesOwned":  issuer_info.get("officerTitle", ""),
            "sharesAmount":     _float(t["transactionShares"]),
            "price":            _float(t["price"]),
            "finalAmount":       t["finalAmount"],
            "isDirector":       issuer_info.get("isDirector", False),
            "name":             issuer_info.get("name", symbol),
        })
    return results


# ── Public API ─────────────────────────────────────────────────────────────────
def get_insider_transactions(days_lookback: int | None = None) -> list[dict]:
    """
    Fetch all recent Form 4 P-Purchase transactions via SEC EDGAR.

    Returns list[{symbol, transactionDate, transactionType, securitiesOwned,
                 sharesAmount, price, finalAmount, isDirector, name}].

    In-process cached — repeated calls cost 0 HTTP requests.
    Only P (Purchase) transactions are returned (FMP's "P-Purchase" signal).

    Args:
        days_lookback: days of filings to return (default: INSIDER_LOOKBACK_DAYS)
    """
    global _cache_transactions
    if _cache_transactions is not None:
        return _cache_transactions

    lookback = days_lookback or INSIDER_LOOKBACK_DAYS
    cutoff   = datetime.date.today() - datetime.timedelta(days=lookback)

    log.info("EDGAR: looking for Form 4 P-Purchase filings (lookback=%dd, cutoff=%s)",
             lookback, cutoff.isoformat())

    # ── Load CIK→ticker map (1 request) ───────────────────────────────────────
    ticker_map = _load_cik_ticker_map()
    log.info("  CIK→ticker map: %d entries", len(ticker_map))

    # ── Collect filing URLs from RSS feed ─────────────────────────────────────
    all_urls: list[str] = []
    seen: set[str] = set()

    for page in range(3):
        params = {
            "action": "getcurrent",
            "type":   "4",
            "owner":  "only",
            "count":  str(_RSS_COUNT),
            "start":  str(page * _RSS_COUNT),
            "output": "atom",
        }
        try:
            r = requests.get(_RSS_URL, params=params, headers=_HEADERS, timeout=20)
            r.raise_for_status()
        except Exception as e:
            log.warning("EDGAR RSS page %d failed: %s", page, e)
            break

        try:
            feed = ET.fromstring(r.text)
        except ET.ParseError:
            break

        ns = "http://www.w3.org/2005/Atom"
        for entry in feed.findall(f".//{{{ns}}}entry"):
            link = entry.find(f".//{{{ns}}}link")
            if link is None:
                continue
            href = link.get("href", "")
            if href and href not in seen:
                seen.add(href)
                all_urls.append(href)

        if page < 2:
            time.sleep(0.25)

    log.info("  RSS: %d unique filing URLs", len(all_urls))

    # Deduplicate by CIK — keep most recent per company
    cik_to_url: dict[str, str] = {}
    for url in all_urls:
        cik = _cik_from_url(url)
        if cik and cik not in cik_to_url:
            cik_to_url[cik] = url

    filing_urls = list(cik_to_url.values())
    log.info("  Unique companies to fetch: %d", len(filing_urls))

    # ── Batch-fetch filings ─────────────────────────────────────────────────────
    all_txns: list[dict] = []
    BATCH      = 80
    BATCH_PAUSE = 13  # seconds between batches (respects SEC 10 req/s limit)

    for i in range(0, len(filing_urls), BATCH):
        batch = filing_urls[i : i + BATCH]
        batch_n = i // BATCH + 1
        batch_total = (len(filing_urls) + BATCH - 1) // BATCH
        log.info(f"  Batch {batch_n}/{batch_total} — fetching {len(batch)} filings…")

        for url in batch:
            cik = _cik_from_url(url)
            ticker = ticker_map.get(cik, "") if cik else ""
            xml_url = _index_to_xml_url(url)
            txns = _fetch_and_parse(xml_url, ticker)
            all_txns.extend(txns)
            time.sleep(0.15)

        if i + BATCH < len(filing_urls):
            log.info("  Sleeping %ds (SEC rate limit)…", BATCH_PAUSE)
            time.sleep(BATCH_PAUSE)

    log.info("  Parsed: %d P-Purchases before date filter", len(all_txns))

    # ── Filter by lookback date ─────────────────────────────────────────────────
    cutoff_dt = cutoff
    filtered: list[dict] = []
    dropped_date = 0
    for t in all_txns:
        tdate_str = t.get("transactionDate", "")
        if tdate_str:
            try:
                if datetime.date.fromisoformat(tdate_str[:10]) < cutoff_dt:
                    dropped_date += 1
                    continue
            except ValueError:
                pass
        filtered.append(t)

    if dropped_date:
        log.info("  Dropped %d by lookback date", dropped_date)

    _cache_transactions = filtered
    log.info("  → %d transactions from EDGAR in lookback window", len(filtered))
    return filtered
