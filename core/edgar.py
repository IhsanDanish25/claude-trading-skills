"""
SEC EDGAR Form 4 insider transactions — zero API key required.

Fetches Form 4 filings from SEC EDGAR, parses P-Purchase transactions.

URL patterns:
  - Newer/XBRL filings: <dir>/ownership.xml   (e.g. 0001193125...-format)
  - Older filings:       <dir>/primarydocument.xml  (older accession format)

Both XML formats use identical field names (issuerTradingSymbol, transactionCode,
transactionShares, etc.) so the same parser works for both.

In-process cached — repeated calls in the same run cost 0 HTTP requests.
"""
from __future__ import annotations

import datetime
import logging
import re
import time
import xml.etree.ElementTree as ET

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


def _xml_url_from_index(index_url: str) -> str:
    """
    Convert an index.htm URL to the best available XML document.

    Index:  https://www.sec.gov/.../CIK/ACCESSION/ACCESSION-COUNT-index.htm

    First tries <ACCESSION>/ownership.xml  (modern XBRL filings)
    Falls back to <ACCESSION>/primarydocument.xml  (older format)

    Returns the full XML URL.
    """
    parts = index_url.rsplit("/", 1)
    if len(parts) != 2:
        return index_url
    dir_path = parts[0]
    # Prefer ownership.xml (modern XBRL); fall back to primarydocument.xml
    # Using tuple to return (xml_url, path) — caller will try ownership.xml, fallback to primary
    return dir_path + "/ownership.xml"


# ── Transaction extraction ─────────────────────────────────────────────────────
_SKIP_SECURITIES = {"preferred", "warrant", "option", "unit", "right",
                    "depositary share", "share unit"}


def _inner_value(elem: ET.Element) -> str:
    """Get text: check elem.text first, then look for <value> child."""
    v = _strip(elem)
    if v:
        return v
    for child in elem:
        if _ltag(child) == "value":
            return _strip(child)
    return ""


def _collect_fields(block: ET.Element) -> dict[str, str]:
    """Recursively collect all named leaf fields from a transaction XML block."""
    result: dict[str, str] = {}

    def _recurse(parent: ET.Element):
        for child in parent:
            tag = _ltag(child)
            # Skip structural container tags — only descend into them
            if tag in ("nonDerivativeTransaction", "derivativeTransaction",
                       "nonDerivativeTable", "derivativeTable", "value",
                       "nonDerivativeHolding", "derivativeHolding"):
                _recurse(child)
                continue
            v = _inner_value(child)
            if v:
                result[tag] = v
            else:
                _recurse(child)

    _recurse(block)
    return result


def _walk_transactions(root: ET.Element):
    """
    Walk ElementTree and yield field-dicts for each transaction block.
    """
    TXN_TAGS = {"nonDerivativeTransaction", "derivativeTransaction"}
    stack: list[ET.Element] = list(root)

    while stack:
        elem = stack.pop()
        t = _ltag(elem)
        if t in TXN_TAGS:
            yield _collect_fields(elem)
        else:
            for child in elem:
                stack.append(child)


def _load_cik_ticker_map() -> dict[str, str]:
    global _cache_cik_map
    if _cache_cik_map is not None:
        return _cache_cik_map

    log.info("EDGAR: loading company_tickers.json")
    try:
        r = requests.get(_TICKERS_URL, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        raw = r.json()
        ticker_map: dict[str, str] = {}
        for entry in raw.values():
            if isinstance(entry, dict) and entry.get("ticker"):
                cik = str(entry["cik_str"]).zfill(10)
                ticker = str(entry["ticker"]).strip()
                if ticker and cik:
                    ticker_map[cik] = ticker
        _cache_cik_map = ticker_map
        log.info("  CIK→ticker map: %d entries", len(ticker_map))
        return ticker_map
    except Exception as e:
        log.warning("EDGAR: company_tickers.json: %s", e)
        _cache_cik_map = {}
        return {}


# ── Single-filing fetch and parse ──────────────────────────────────────────────
def _fetch_and_parse(index_url: str, ticker_fallback: str) -> list[dict]:
    """
    Fetch a Form 4 filing, parse P-Purchase transactions, return FMP-shaped dicts.

    Args:
        index_url: The -index.htm URL from the RSS feed
        ticker_fallback: ticker from CIK→ticker map (used if XML has no ticker)
    Returns:
        list of transaction dicts in FMP shape, or [] on failure.
    SEC rate limit: caller enforces 0.15s between calls.
    """
    # Build XML URL: ownership.xml (modern) + primarydocument.xml (legacy) fallback
    parts = index_url.rsplit("/", 1)
    if len(parts) != 2:
        return []
    dir_path = parts[0]

    xml_urls = [
        f"{dir_path}/ownership.xml",          # modern XBRL filings
        f"{dir_path}/primarydocument.xml",   # older filings
    ]

    xml_text = None
    used_url = None
    for url in xml_urls:
        try:
            r = requests.get(url, headers=_HEADERS, timeout=20)
            if r.status_code == 429:
                log.debug("EDGAR rate-limited (429): %s", url)
                return []
            if r.status_code == 200 and len(r.text) > 500:
                xml_text = r.text
                used_url = url
                break
        except Exception as e:
            log.debug("EDGAR fetch %s: %s", url, e)
            continue

    if not xml_text:
        return []

    # Parse XML
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        log.debug("EDGAR: XML parse error on %s", used_url)
        return []

    # ── Issuer info ──────────────────────────────────────────────────────────
    symbol = ""
    for elem in root.iter():
        if _ltag(elem) == "issuerTradingSymbol":
            symbol = _inner_value(elem)
            break

    if not symbol:
        symbol = ticker_fallback
    if not symbol:
        return []

    # Extract owner name, director flag, officer title
    is_director = False
    owner_name = ""
    officer_title = ""
    for block in root.iter():
        if _ltag(block) == "reportingOwnerRelationship":
            if _inner_value(block.find("isDirector") or block).lower() == "true":
                is_director = True
            t = _inner_value(block.find("officerTitle") or block)
            if t:
                officer_title = t
        if _ltag(block) == "reportingOwnerId":
            n = _inner_value(block.find("rptOwnerName") or block)
            if n:
                owner_name = n

    if not owner_name:
        owner_name = symbol

    # periodOfReport = the filing/transaction date anchor
    period_of_report = ""
    for elem in root.iter():
        if _ltag(elem) == "periodOfReport":
            period_of_report = _inner_value(elem)
            break

    # ── Extract transactions ──────────────────────────────────────────────────
    results: list[dict] = []
    for txn in _walk_transactions(root):
        code = txn.get("transactionCode", "")
        if code != "P":          # Only P = Purchase (our signal)
            continue

        sec_title = txn.get("securityTitle", "")
        if sec_title:
            if any(kw in sec_title.lower() for kw in _SKIP_SECURITIES):
                continue

        shares = _float(txn.get("transactionShares", ""))
        price   = _float(txn.get("transactionPricePerShare", ""))
        amount  = shares * price if shares and price else 0.0

        txn_date = txn.get("transactionDate", period_of_report)
        if not txn_date:
            txn_date = period_of_report

        shares_str = txn.get("transactionShares", "0")
        price_str  = txn.get("transactionPricePerShare", "0")

        results.append({
            "symbol":           symbol,
            "transactionDate":  txn_date[:10] if txn_date else "",
            "transactionType":  "P-Purchase",
            "securitiesOwned":  officer_title,
            "sharesAmount":     _float(shares_str),
            "price":            _float(price_str),
            "finalAmount":       amount,
            "isDirector":       is_director,
            "name":             owner_name,
        })

    return results


# ── Public API ─────────────────────────────────────────────────────────────────
def get_insider_transactions(days_lookback: int | None = None) -> list[dict]:
    """
    Fetch Form 4 P-Purchase transactions from SEC EDGAR.

    Returns list[{symbol, transactionDate, transactionType, securitiesOwned,
                 sharesAmount, price, finalAmount, isDirector, name}].

    In-process cached — repeated calls within the same run are free.
    """
    global _cache_transactions
    if _cache_transactions is not None:
        return _cache_transactions

    lookback = days_lookback or INSIDER_LOOKBACK_DAYS
    cutoff   = datetime.date.today() - datetime.timedelta(days=lookback)

    log.info(f"EDGAR: fetching Form 4 P-Purchases (lookback={lookback}d, cutoff={cutoff})")

    # Load CIK→ticker map (1 request)
    ticker_map = _load_cik_ticker_map()

    # Fetch RSS feed for Form 4 filing URLs
    all_urls: list[str] = []
    seen: set[str] = set()

    for page in range(3):
        params = {
            "action": "getcurrent", "type": "4", "owner": "only",
            "count": str(_RSS_COUNT), "start": str(page * _RSS_COUNT),
            "output": "atom",
        }
        try:
            r = requests.get(_RSS_URL, params=params, headers=_HEADERS, timeout=20)
            r.raise_for_status()
        except Exception as e:
            log.warning("EDGAR RSS page %d: %s", page, e)
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

    # Deduplicate by CIK
    cik_to_url: dict[str, str] = {}
    for url in all_urls:
        cik = _cik_from_url(url)
        if cik and cik not in cik_to_url:
            cik_to_url[cik] = url

    filing_urls = list(cik_to_url.values())
    log.info("  Unique companies: %d", len(filing_urls))

    # Batch fetch filings with SEC rate limits
    all_txns: list[dict] = []
    BATCH      = 80
    BATCH_PAUSE = 6   # 80 calls × 0.1s = 8s, use 6s pause

    for i in range(0, len(filing_urls), BATCH):
        batch = filing_urls[i : i + BATCH]
        batch_n = i // BATCH + 1
        log.info(f"  Batch {batch_n} — fetching {len(batch)} filings…")

        for url in batch:
            cik    = _cik_from_url(url)
            ticker = ticker_map.get(cik, "") if cik else ""
            txns   = _fetch_and_parse(url, ticker)
            all_txns.extend(txns)
            time.sleep(0.15)

        if i + BATCH < len(filing_urls):
            log.info("  Sleep %ds (SEC rate limit)", BATCH_PAUSE)
            time.sleep(BATCH_PAUSE)

    # Date filter
    cutoff_dt = cutoff
    filtered: list[dict] = []
    dropped  = 0
    for t in all_txns:
        tdate_str = t.get("transactionDate", "")
        if tdate_str:
            try:
                if datetime.date.fromisoformat(tdate_str[:10]) < cutoff_dt:
                    dropped += 1
                    continue
            except ValueError:
                pass
        filtered.append(t)

    _cache_transactions = filtered
    log.info("  Parsed %d P-Purchases, %d within lookback window", len(all_txns), len(filtered))
    return filtered
