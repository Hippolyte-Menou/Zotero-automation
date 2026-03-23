"""Batch-download PDFs for Zotero items missing full text.

Fetches items from a group library filtered by tag (default: source:deep-search),
identifies those without PDF attachments, and attempts to download PDFs using
multiple strategies:

  1. Unpaywall API  -- free OA full-text links (no auth needed)
  2. PubMed Central -- direct PMC PDF endpoint if PMCID available
  3. Crossref + headless browser -- gets real PDF URLs from Crossref metadata,
     then downloads via Playwright (handles JS, cookies, bot detection).
     Requires campus network or VPN for paywalled content.

Downloaded PDFs are attached to the parent item via pyzotero.

API key:    ../zotero-tools/.zotero-api-key  (or ZOTERO_API_KEY env var)
Group ID:   6432168 (hardcoded)

Usage:
    python batch_fetch_pdfs.py                          # default tag
    python batch_fetch_pdfs.py --tag "source:deep-search"
    python batch_fetch_pdfs.py --dry-run                # list items, don't download
    python batch_fetch_pdfs.py --limit 20               # process first N items only
    python batch_fetch_pdfs.py --no-browser             # skip headless browser strategy
    python batch_fetch_pdfs.py -v                       # verbose debug output
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import requests
from pyzotero import zotero

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# API key file (shared with zotero-tools scripts)
_SCRIPT_DIR = Path(__file__).resolve().parent
_API_KEY_FILE = _SCRIPT_DIR.parent / "zotero-tools" / ".zotero-api-key"

ZOTERO_GROUP_ID = "6432168"
UNPAYWALL_EMAIL = "h.menou@ucl.ac.uk"

# Unpaywall API (free, 100k requests/day)
UNPAYWALL_API = "https://api.unpaywall.org/v2/{doi}?email={email}"

# PMC PDF endpoint
PMC_PDF_URL = "https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"

# Crossref API
CROSSREF_API = "https://api.crossref.org/works/{doi}"

# Polite headers for API requests
API_HEADERS = {
    "User-Agent": f"batch_fetch_pdfs/2.0 (mailto:{UNPAYWALL_EMAIL})",
}


def load_api_key() -> str:
    """Load Zotero API key from file, falling back to env var."""
    if _API_KEY_FILE.exists():
        key = _API_KEY_FILE.read_text().strip()
        if key:
            logger.debug(f"API key loaded from {_API_KEY_FILE}")
            return key
    key = os.environ.get("ZOTERO_API_KEY", "")
    if key:
        logger.debug("API key loaded from ZOTERO_API_KEY env var")
        return key
    logger.error(
        f"No API key found. Place it in {_API_KEY_FILE} "
        "or set ZOTERO_API_KEY env var."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Zotero helpers
# ---------------------------------------------------------------------------

def _item_has_pdf(zot: zotero.Zotero, item_key: str) -> bool:
    """Check if an item already has a PDF attachment (per-item query)."""
    try:
        children = zot.children(item_key)
        for child in children:
            ct = child["data"].get("contentType", "")
            if "pdf" in ct.lower():
                return True
    except Exception as e:
        logger.debug(f"Error checking children for {item_key}: {e}")
    return False


def iter_items_missing_pdfs(
    zot: zotero.Zotero,
    tag: str,
    limit: int = 0,
) -> list[dict]:
    """Yield items with the given tag that lack PDF attachments.

    Fetches items in pages and checks attachments per-item, stopping
    once `limit` missing items are found (0 = no limit).
    """
    logger.info(f"Scanning items with tag '{tag}' for missing PDFs...")
    missing: list[dict] = []
    start = 0
    page_size = 100
    total_scanned = 0

    while True:
        page = zot.items(tag=tag, itemType="-attachment", limit=page_size, start=start)
        if not page:
            break

        for item in page:
            total_scanned += 1
            key = item["data"]["key"]
            if not _item_has_pdf(zot, key):
                missing.append(item)
                title = item["data"].get("title", "")[:50]
                logger.info(
                    f"  [{len(missing)}] missing PDF: {title}... "
                    f"(scanned {total_scanned})"
                )
                if limit > 0 and len(missing) >= limit:
                    logger.info(
                        f"Reached limit of {limit} missing items "
                        f"(scanned {total_scanned} total)"
                    )
                    return missing

        if len(page) < page_size:
            break
        start += page_size

    logger.info(
        f"Found {len(missing)} items missing PDFs "
        f"(scanned {total_scanned} total)"
    )
    return missing


def extract_doi(item: dict) -> str | None:
    """Extract DOI from a Zotero item."""
    doi = item["data"].get("DOI", "").strip()
    if doi:
        doi = re.sub(r"^https?://doi\.org/", "", doi)
        return doi
    url = item["data"].get("url", "")
    m = re.search(r"doi\.org/(10\.\S+)", url)
    return m.group(1) if m else None


def extract_pmcid(item: dict) -> str | None:
    """Extract PMCID from a Zotero item's extra field."""
    extra = item["data"].get("extra", "")
    m = re.search(r"PMCID:\s*(PMC\d+)", extra)
    return m.group(1) if m else None


def format_title(item: dict, max_len: int = 60) -> str:
    title = item["data"].get("title", "(no title)")
    if len(title) > max_len:
        title = title[: max_len - 3] + "..."
    return title


# ---------------------------------------------------------------------------
# Strategy 1: Unpaywall (OA articles)
# ---------------------------------------------------------------------------

def try_unpaywall(doi: str, session: requests.Session) -> str | None:
    """Query Unpaywall for an OA PDF link."""
    url = UNPAYWALL_API.format(doi=doi, email=UNPAYWALL_EMAIL)
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        best = data.get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf")
        if pdf_url:
            return pdf_url
        for loc in data.get("oa_locations", []):
            if loc.get("url_for_pdf"):
                return loc["url_for_pdf"]
    except Exception as e:
        logger.debug(f"Unpaywall error for {doi}: {e}")
    return None


# ---------------------------------------------------------------------------
# Strategy 2: PubMed Central
# ---------------------------------------------------------------------------

def try_pmc(pmcid: str, session: requests.Session) -> str | None:
    """Try to get PDF from PubMed Central."""
    url = PMC_PDF_URL.format(pmcid=pmcid)
    try:
        resp = session.head(url, allow_redirects=True, timeout=15)
        ct = resp.headers.get("Content-Type", "")
        if resp.ok and "pdf" in ct.lower():
            return url
    except Exception as e:
        logger.debug(f"PMC error for {pmcid}: {e}")
    return None


# ---------------------------------------------------------------------------
# Strategy 3: Crossref metadata -> headless browser download
# ---------------------------------------------------------------------------

def get_pdf_urls_from_crossref(doi: str, session: requests.Session) -> list[str]:
    """Query Crossref for PDF URLs associated with a DOI.

    Returns URLs ordered by preference:
      1. similarity-checking links (usually the real publisher PDF)
      2. text-mining links with application/pdf
      3. constructed ScienceDirect/Wiley/etc. URLs from the DOI
    """
    urls: list[str] = []
    try:
        resp = session.get(
            CROSSREF_API.format(doi=doi),
            timeout=15,
            headers=API_HEADERS,
        )
        if resp.status_code == 200:
            links = resp.json().get("message", {}).get("link", [])
            # Prefer similarity-checking (these are actual PDF endpoints)
            for link in links:
                if link.get("intended-application") == "similarity-checking":
                    u = link.get("URL", "")
                    if u:
                        urls.append(u)
            # Then text-mining PDF links
            for link in links:
                ct = link.get("content-type", "")
                if "pdf" in ct and link.get("URL") not in urls:
                    urls.append(link["URL"])
    except Exception as e:
        logger.debug(f"Crossref error for {doi}: {e}")

    # Construct fallback URLs from DOI for known publishers.
    # These are last-resort; Crossref links above are preferred.
    doi_lower = doi.lower()
    if "10.1016/" in doi_lower:
        # Elsevier: go through DOI resolver (will redirect to ScienceDirect)
        # Can't construct PII from DOI; must follow redirect chain
        urls.append(f"https://doi.org/{doi}")
    if "10.1111/" in doi_lower or "10.1002/" in doi_lower:
        # Wiley: /doi/pdfdirect/ serves PDF directly if you have access
        urls.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}")
    if "10.1007/" in doi_lower:
        # Springer
        urls.append(f"https://link.springer.com/content/pdf/{doi}.pdf")
    if "10.1038/" in doi_lower:
        # Nature
        urls.append(f"https://www.nature.com/articles/{doi.split('/')[-1]}.pdf")
    if "10.1080/" in doi_lower or "10.1167/" in doi_lower:
        # Taylor & Francis / ARVO
        urls.append(f"https://doi.org/{doi}")

    return urls


def _init_browser():
    """Launch a Playwright browser for PDF downloads."""
    from playwright.sync_api import sync_playwright  # noqa: delay import

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        accept_downloads=True,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    )
    return pw, browser, context


def try_browser_download(
    urls: list[str],
    tmpdir: str,
    filename: str,
    context,
) -> str | None:
    """Try downloading a PDF from a list of URLs using a headless browser.

    For each URL, navigates to it, checks the response content-type,
    and if it's a PDF, saves it. Handles JavaScript redirects and cookies.
    Uses 'domcontentloaded' (not 'networkidle') to avoid timeouts on
    publisher pages that load scripts endlessly.
    """
    page = context.new_page()
    filepath = os.path.join(tmpdir, filename)

    for url in urls:
        try:
            logger.debug(f"  Browser trying: {url[:80]}")

            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if resp is None:
                continue

            ct = resp.headers.get("content-type", "")
            status = resp.status

            # Direct PDF response
            if "pdf" in ct.lower() and status == 200:
                body = resp.body()
                if body[:5] == b"%PDF-":
                    with open(filepath, "wb") as f:
                        f.write(body)
                    page.close()
                    return filepath

            # Access denied -- no point trying PDF buttons on a login page
            if status in (401, 403, 407):
                logger.debug(f"  Access denied ({status}) for {url[:60]}")
                continue

            # Landed on HTML page -- look for PDF download links
            # Wait briefly for JS-rendered content
            page.wait_for_timeout(2000)

            for selector in [
                'a[href*="/pdf"]',
                'a[href$=".pdf"]',
                'a:has-text("Download PDF")',
                'a:has-text("Get PDF")',
                'a:has-text("View PDF")',
            ]:
                try:
                    link = page.query_selector(selector)
                    if not link:
                        continue
                    href = link.get_attribute("href")
                    if not href:
                        continue
                    # Make absolute URL
                    if href.startswith("/"):
                        from urllib.parse import urlparse
                        parsed = urlparse(page.url)
                        href = f"{parsed.scheme}://{parsed.netloc}{href}"
                    elif not href.startswith("http"):
                        continue

                    resp2 = page.goto(href, wait_until="domcontentloaded", timeout=20000)
                    if resp2 and "pdf" in resp2.headers.get("content-type", "").lower():
                        body2 = resp2.body()
                        if body2[:5] == b"%PDF-":
                            with open(filepath, "wb") as f:
                                f.write(body2)
                            page.close()
                            return filepath
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"  Browser error on {url[:60]}: {e}")
            continue

    page.close()
    return None


# ---------------------------------------------------------------------------
# Download + validation helpers
# ---------------------------------------------------------------------------

def download_pdf_simple(
    pdf_url: str,
    session: requests.Session,
    tmpdir: str,
    filename: str,
) -> str | None:
    """Download a PDF via requests. Works for OA/PMC content."""
    try:
        resp = session.get(pdf_url, stream=True, timeout=60)
        if not resp.ok:
            return None

        ct = resp.headers.get("Content-Type", "")
        if "html" in ct.lower():
            return None

        filepath = os.path.join(tmpdir, filename)
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        if _is_valid_pdf(filepath):
            return filepath
        _safe_remove(filepath)
        return None

    except Exception as e:
        logger.debug(f"Download error from {pdf_url}: {e}")
        return None


def _is_valid_pdf(filepath: str) -> bool:
    """Check if a file starts with the PDF magic bytes."""
    try:
        with open(filepath, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def _safe_remove(filepath: str) -> None:
    try:
        os.remove(filepath)
    except OSError:
        pass


def attach_pdf_to_item(zot: zotero.Zotero, item_key: str, pdf_path: str) -> bool:
    """Upload a PDF as a child attachment of the given item."""
    try:
        result = zot.attachment_simple([pdf_path], item_key)
        if result.get("success"):
            return True
        if result.get("failure"):
            logger.warning(f"Attachment upload failed for {item_key}: {result['failure']}")
            return False
        if result.get("unchanged"):
            logger.info(f"Attachment already exists for {item_key}")
            return True
        return True
    except Exception as e:
        logger.error(f"Attachment upload error for {item_key}: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Batch-download PDFs for Zotero items missing full text"
    )
    parser.add_argument(
        "--tag", default="source:deep-search",
        help="Zotero tag to filter items (default: source:deep-search)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List items that need PDFs without downloading",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Process at most N items (0 = all)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Skip headless browser strategy (Unpaywall + PMC only)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    api_key = load_api_key()

    # Connect to Zotero
    zot = zotero.Zotero(ZOTERO_GROUP_ID, "group", api_key)

    # Incrementally find items missing PDFs (stops at --limit)
    missing = iter_items_missing_pdfs(zot, args.tag, limit=args.limit)
    if not missing:
        logger.info("No items missing PDFs (or none found). Done.")
        return

    # Dry run
    if args.dry_run:
        print(f"\n{'='*70}")
        print(f"  {len(missing)} items missing PDFs (tag: {args.tag})")
        print(f"{'='*70}\n")
        for i, it in enumerate(missing, 1):
            doi = extract_doi(it) or "(no DOI)"
            pmcid = extract_pmcid(it) or ""
            pmc_str = f"  [{pmcid}]" if pmcid else ""
            print(f"  {i:3d}. {format_title(it)}")
            print(f"       DOI: {doi}{pmc_str}")
        return

    # HTTP session for Unpaywall/PMC/Crossref API calls
    session = requests.Session()
    session.headers.update(API_HEADERS)

    # Browser context (lazy init)
    pw = browser = browser_context = None
    use_browser = not args.no_browser

    stats = {"unpaywall": 0, "pmc": 0, "browser": 0, "failed": 0, "no_doi": 0}

    try:
        with tempfile.TemporaryDirectory(prefix="zotero_pdfs_") as tmpdir:
            for i, item in enumerate(missing, 1):
                key = item["data"]["key"]
                title = format_title(item)
                doi = extract_doi(item)
                pmcid = extract_pmcid(item)

                logger.info(f"[{i}/{len(missing)}] {title}")

                if not doi and not pmcid:
                    logger.warning(f"  No DOI or PMCID -- skipping")
                    stats["no_doi"] += 1
                    continue

                safe_name = (doi or pmcid or key).replace("/", "_")
                filename = f"{safe_name}.pdf"
                filepath = None
                source = None

                # Strategy 1: Unpaywall (OA)
                if doi:
                    pdf_url = try_unpaywall(doi, session)
                    if pdf_url:
                        filepath = download_pdf_simple(pdf_url, session, tmpdir, filename)
                        if filepath:
                            source = "unpaywall"

                # Strategy 2: PMC
                if not filepath and pmcid:
                    pdf_url = try_pmc(pmcid, session)
                    if pdf_url:
                        filepath = download_pdf_simple(pdf_url, session, tmpdir, filename)
                        if filepath:
                            source = "pmc"

                # Strategy 3: Crossref + headless browser
                if not filepath and doi and use_browser:
                    pdf_urls = get_pdf_urls_from_crossref(doi, session)
                    if pdf_urls:
                        # Lazy-init browser on first use
                        if browser_context is None:
                            logger.info("Launching headless browser...")
                            pw, browser, browser_context = _init_browser()
                        filepath = try_browser_download(
                            pdf_urls, tmpdir, filename, browser_context,
                        )
                        if filepath:
                            source = "browser"

                if not filepath:
                    logger.warning(f"  No PDF found")
                    stats["failed"] += 1
                    time.sleep(0.5)
                    continue

                logger.info(f"  Found via {source}")

                # Attach to Zotero
                if attach_pdf_to_item(zot, key, filepath):
                    logger.info(f"  Attached successfully")
                    stats[source] += 1
                else:
                    logger.warning(f"  Attachment upload failed")
                    stats["failed"] += 1

                # Clean up temp file after upload
                _safe_remove(filepath)

                # Rate limit
                time.sleep(1.0)

    finally:
        # Clean up browser
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass

    # Summary
    total_ok = stats["unpaywall"] + stats["pmc"] + stats["browser"]
    print(f"\n{'='*50}")
    print(f"  Batch PDF fetch complete")
    print(f"{'='*50}")
    print(f"  Unpaywall:    {stats['unpaywall']:4d}")
    print(f"  PMC:          {stats['pmc']:4d}")
    print(f"  Browser:      {stats['browser']:4d}")
    print(f"  -------------------------")
    print(f"  Total fetched:{total_ok:4d}")
    print(f"  Failed:       {stats['failed']:4d}")
    print(f"  No DOI/PMCID: {stats['no_doi']:4d}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
