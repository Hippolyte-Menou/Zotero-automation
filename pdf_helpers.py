"""Zotero helpers and PDF utilities for batch PDF fetching.

Shared infrastructure used by the central dispatcher and individual
PDF strategies: Zotero scanning, DOI/PMCID extraction, attachment
linking, download, and validation.
"""

import copy
import logging
import os
import re
from pathlib import Path

import requests
from pyzotero import zotero

from bio_toolkit.config import ZOTERO_GROUP_ID, zotero_api_key
from bio_toolkit.util.cache import pdf_corpus_dir

logger = logging.getLogger(__name__)

# Shared PDF corpus = literature/pdfs/ (under the Zotero Linked Attachment Base
# Directory, where ZotMoov parks the citekey-named PDFs). Resolved via the
# toolkit so every project agrees on the location; OneDrive-synced, never
# git-tracked.
PDF_SAVE_DIR = pdf_corpus_dir()
UNPAYWALL_EMAIL = "h.menou@ucl.ac.uk"

# Polite headers for API requests
API_HEADERS = {
    "User-Agent": f"batch_fetch_pdfs/2.0 (mailto:{UNPAYWALL_EMAIL})",
}


def load_api_key() -> str:
    """Return the Zotero API key.

    Thin wrapper over bio_toolkit.config.zotero_api_key() kept so existing call
    sites keep working. The key now comes from the ZOTERO_API_KEY env var or the
    toolkit's gitignored secret file; the old per-bot `.zotero-api-key` path is
    gone (it broke on relocation out of the vault tree).
    """
    return zotero_api_key()


# ---------------------------------------------------------------------------
# Zotero scanning
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
    """Return items with the given tag that lack PDF attachments.

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


# ---------------------------------------------------------------------------
# Item field extraction
# ---------------------------------------------------------------------------

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


def safe_filename(item: dict) -> str:
    """Generate a PDF filename from the item's citationKey, falling back to DOI/PMCID/key."""
    ck = item["data"].get("citationKey", "").strip()
    if ck:
        return f"{ck}.pdf"
    doi = extract_doi(item)
    pmcid = extract_pmcid(item)
    key = item["data"]["key"]
    fallback = (doi or pmcid or key).replace("/", "_").replace(":", "_")
    logger.warning(f"No citationKey for item {key} -- using fallback filename: {fallback}.pdf")
    return f"{fallback}.pdf"


# ---------------------------------------------------------------------------
# Download + validation
# ---------------------------------------------------------------------------

def download_pdf_simple(
    pdf_url: str,
    session: requests.Session,
    save_dir: str,
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

        filepath = os.path.join(save_dir, filename)
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        if is_valid_pdf(filepath):
            return filepath
        safe_remove(filepath)
        return None

    except Exception as e:
        logger.debug(f"Download error from {pdf_url}: {e}")
        return None


def is_valid_pdf(filepath: str) -> bool:
    """Check if a file starts with the PDF magic bytes."""
    try:
        with open(filepath, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def safe_remove(filepath: str) -> None:
    try:
        os.remove(filepath)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Zotero attachment
# ---------------------------------------------------------------------------

_linked_file_template: dict | None = None


def link_pdf_to_item(zot: zotero.Zotero, item_key: str, pdf_path: str) -> bool:
    """Create a linked-file attachment pointing to pdf_path on disk."""
    global _linked_file_template
    try:
        if _linked_file_template is None:
            _linked_file_template = zot.item_template("attachment", "linked_file")
        template = copy.deepcopy(_linked_file_template)
        template["title"] = Path(pdf_path).name
        template["path"] = str(Path(pdf_path).resolve())
        template["contentType"] = "application/pdf"
        result = zot.create_items([template], parentid=item_key)
        if result.get("success"):
            return True
        if result.get("failure"):
            logger.warning(f"Linked attachment failed for {item_key}: {result['failure']}")
            return False
        return True
    except Exception as e:
        logger.error(f"Linked attachment error for {item_key}: {e}")
        return False
