"""Central dispatcher for batch PDF fetching.

Tries PDF fetching strategies in order for each Zotero item missing a PDF.
Usable both as a CLI tool and as an importable module.

CLI usage:
    python fetch_pdfs.py                          # default tag
    python fetch_pdfs.py -v --tag "source:deep-search" --limit 100 
    python fetch_pdfs.py --dry-run                # list items, don't download
    python fetch_pdfs.py --limit 20               # process first N items only
    python fetch_pdfs.py --no-browser             # skip headless browser strategy
    python fetch_pdfs.py -v                       # verbose debug output

Module usage:
    from pdf_helpers import iter_items_missing_pdfs, load_api_key
    from pdf_strategies import UnpaywallStrategy, PmcStrategy
    from fetch_pdfs import fetch_pdfs

    zot = Zotero(GROUP_ID, "group", load_api_key())
    items = iter_items_missing_pdfs(zot, tag="source:deep-search")
    stats = fetch_pdfs(
        items,
        strategies=[UnpaywallStrategy(), PmcStrategy()],
        session=requests.Session(),
        save_dir="/path/to/pdfs",
        zot=zot,
    )
"""

import argparse
import logging
import time
from pathlib import Path

import requests
from pyzotero import zotero

from pdf_helpers import (
    ZOTERO_GROUP_ID,
    PDF_SAVE_DIR,
    API_HEADERS,
    load_api_key,
    iter_items_missing_pdfs,
    extract_doi,
    extract_pmcid,
    format_title,
    safe_filename,
    link_pdf_to_item,
)
from pdf_strategies import PdfStrategy, BROWSER_STRATEGIES
from pdf_strategies.unpaywall import UnpaywallStrategy
from pdf_strategies.pmc import PmcStrategy

logger = logging.getLogger(__name__)


def fetch_pdfs(
    items: list[dict],
    strategies: list[PdfStrategy],
    session: requests.Session,
    save_dir: str,
    zot: zotero.Zotero | None = None,
    rate_limit: float = 1.0,
) -> dict:
    """Try strategies in order for each item. Returns stats dict.

    Args:
        items: Zotero item dicts to fetch PDFs for.
        strategies: Ordered list of PdfStrategy instances to try.
        session: Shared requests.Session with polite headers.
        save_dir: Directory to save PDFs to.
        zot: If provided, links downloaded PDFs to Zotero items.
        rate_limit: Seconds to wait between items.

    Returns:
        Dict with per-strategy success counts, plus 'failed', 'no_doi',
        and 'skipped' counts.
    """
    stats = {s.name: 0 for s in strategies}
    stats.update({"failed": 0, "no_doi": 0, "skipped": 0})

    try:
        for i, item in enumerate(items, 1):
            key = item["data"]["key"]
            title = format_title(item)
            doi = extract_doi(item)
            pmcid = extract_pmcid(item)

            logger.info(f"[{i}/{len(items)}] {title}")

            if not doi and not pmcid:
                logger.warning(f"  No DOI or PMCID -- skipping")
                stats["no_doi"] += 1
                continue

            filename = safe_filename(item)
            dest_path = Path(save_dir) / filename

            if dest_path.exists():
                logger.info(f"  Already on disk: {filename}")
                if zot and link_pdf_to_item(zot, key, str(dest_path)):
                    logger.info(f"  Linked successfully")
                stats["skipped"] += 1
                time.sleep(0.5)
                continue

            filepath = None
            source = None
            for strategy in strategies:
                filepath = strategy.try_fetch(item, session, save_dir)
                if filepath:
                    source = strategy.name
                    break

            if not filepath:
                logger.warning(f"  No PDF found")
                stats["failed"] += 1
                time.sleep(0.5)
                continue

            logger.info(f"  Saved via {source}: {filename}")

            if zot:
                if link_pdf_to_item(zot, key, filepath):
                    logger.info(f"  Linked successfully")
                    stats[source] += 1
                else:
                    logger.warning(f"  Zotero link failed (file kept on disk)")
                    stats["failed"] += 1
            else:
                stats[source] += 1

            time.sleep(rate_limit)

    finally:
        for strategy in strategies:
            strategy.cleanup()

    return stats


def _print_summary(stats: dict, strategies: list[PdfStrategy]) -> None:
    """Print a summary table of fetch results."""
    total_ok = sum(stats.get(s.name, 0) for s in strategies)
    already = stats.get("skipped", 0)
    print(f"\n{'='*50}")
    print(f"  Batch PDF fetch complete")
    print(f"  Save dir: {PDF_SAVE_DIR}")
    print(f"{'='*50}")
    for s in strategies:
        label = f"  {s.name.capitalize()}:"
        print(f"{label:<18}{stats.get(s.name, 0):4d}")
    print(f"  Already saved:{already:4d}")
    print(f"  -------------------------")
    print(f"  Total linked: {total_ok + already:4d}")
    print(f"  Failed:       {stats['failed']:4d}")
    print(f"  No DOI/PMCID: {stats['no_doi']:4d}")
    print(f"{'='*50}\n")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

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

    PDF_SAVE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Saving PDFs to: {PDF_SAVE_DIR}")

    zot = zotero.Zotero(ZOTERO_GROUP_ID, "group", api_key)

    missing = iter_items_missing_pdfs(zot, args.tag, limit=args.limit)
    if not missing:
        logger.info("No items missing PDFs (or none found). Done.")
        return

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

    session = requests.Session()
    session.headers.update(API_HEADERS)

    strategies: list[PdfStrategy] = [UnpaywallStrategy(), PmcStrategy()]
    if not args.no_browser:
        strategies.extend(cls() for cls in BROWSER_STRATEGIES)

    stats = fetch_pdfs(
        items=missing,
        strategies=strategies,
        session=session,
        save_dir=str(PDF_SAVE_DIR),
        zot=zot,
    )

    _print_summary(stats, strategies)


if __name__ == "__main__":
    main()
