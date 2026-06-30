"""Export the Zotero GROUP library's stored PDF attachments into the shared corpus.

Context — read before running:

  * A Zotero *group* library cannot hold linked-file attachments. This is a
    Zotero data-model rule (not a sharing/permissions setting): group files are
    expected to live in Zotero storage so every member can fetch them. The
    consequence is that the group's PDFs cannot be "moved" stored -> linked the
    way ZotMoov moves My Library. So instead of moving, we *copy* each group PDF
    out to literature/pdfs/<citekey>.pdf and leave the group attachment exactly
    as it is.

  * Group file sharing is disabled here, so the bytes live ONLY in local Zotero
    storage (<data-dir>/storage/<attachmentKey>/<filename>). The cloud Web API
    has the *metadata* (items, parents, citation keys) but NOT the *files*. So
    this script reads the Web API strictly READ-ONLY, for metadata only, and
    pulls every byte from local disk. It never creates, updates, links,
    downloads, trashes, or deletes anything in Zotero — the only writes it makes
    are new files under the corpus directory.

  * Tradeoff: each exported PDF then exists twice on disk — once in Zotero
    storage/ (still managed by the group library) and once in the corpus. That
    duplication is unavoidable if you want both a Zotero-managed group AND a flat
    citekey-named corpus that the pipelines / Obsidian read.

CLI:
    python export_group_pdfs.py --dry-run          # report only, write nothing
    python export_group_pdfs.py --limit 5          # first 5 (smoke test)
    python export_group_pdfs.py                     # export all
    python export_group_pdfs.py --force            # overwrite existing corpus files
    python export_group_pdfs.py --storage-dir PATH # override local storage location
"""

import argparse
import logging
import shutil
import time
from pathlib import Path

from pyzotero import zotero

from pdf_helpers import ZOTERO_GROUP_ID, load_api_key
from bio_toolkit.util.cache import pdf_corpus_dir

logger = logging.getLogger(__name__)

# linkMode values for *stored* attachments (files in Zotero storage). pyzotero
# returns linkMode as a string, not the integer the in-app JS API uses.
STORED_LINK_MODES = {"imported_file", "imported_url"}
DEFAULT_STORAGE_DIR = Path.home() / "Zotero" / "storage"


def _paginate(method, label, page_size=100, **kwargs):
    """Yield every item from a pyzotero listing endpoint, page_size at a time."""
    start = 0
    n = 0
    while True:
        page = method(limit=page_size, start=start, **kwargs)
        if not page:
            break
        yield from page
        n += len(page)
        if len(page) < page_size:
            break
        start += page_size
        time.sleep(0.2)
    logger.debug("%s: paginated %d records", label, n)


def collect_stored_pdf_attachments(zot):
    """Return group attachment item dicts that are stored PDFs."""
    atts = []
    for item in _paginate(zot.items, "attachments", itemType="attachment"):
        data = item.get("data", {})
        if data.get("contentType") != "application/pdf":
            continue
        if data.get("linkMode") not in STORED_LINK_MODES:
            continue
        atts.append(item)
    logger.info("Found %d stored PDF attachments in the group library", len(atts))
    return atts


def fetch_parent_citekeys(zot, parent_keys):
    """Batch-fetch {item_key: citationKey} for the given parent keys (read-only).

    Uses the Web API itemKey filter (up to 50 keys per request).
    """
    mapping = {}
    keys = sorted(k for k in parent_keys if k)
    for i in range(0, len(keys), 50):
        batch = keys[i:i + 50]
        items = zot.items(itemKey=",".join(batch), limit=50)
        for it in items:
            data = it.get("data", {})
            mapping[data.get("key", "")] = (data.get("citationKey") or "").strip()
        time.sleep(0.3)
    have = sum(1 for v in mapping.values() if v)
    logger.info("Resolved citekeys for %d/%d parents", have, len(keys))
    return mapping


def local_file_for(att, storage_dir):
    """Return the local stored-file path for an attachment, or None if absent."""
    data = att.get("data", {})
    key = data.get("key", "")
    filename = data.get("filename", "")
    if not key or not filename:
        return None
    candidate = storage_dir / key / filename
    return candidate if candidate.is_file() else None


def export_group_pdfs(
    storage_dir,
    corpus_dir,
    dry_run=False,
    limit=0,
    force=False,
):
    """Copy group stored PDFs into the corpus, named by parent citation key."""
    zot = zotero.Zotero(ZOTERO_GROUP_ID, "group", load_api_key())

    attachments = collect_stored_pdf_attachments(zot)
    if limit:
        attachments = attachments[:limit]

    parent_keys = {a.get("data", {}).get("parentItem", "") for a in attachments}
    citekeys = fetch_parent_citekeys(zot, parent_keys)

    corpus_dir.mkdir(parents=True, exist_ok=True)

    stats = {
        "exported": 0, "skipped_exists": 0, "no_citekey": 0,
        "missing_local": 0, "collision": 0, "failed": 0,
    }
    claimed = {}  # dest filename -> attachment key that claimed it this run

    for att in attachments:
        data = att.get("data", {})
        att_key = data.get("key", "")
        parent = data.get("parentItem", "")
        citekey = citekeys.get(parent, "")

        if not citekey:
            logger.warning("  no citekey: att %s (parent %s) — %s",
                           att_key, parent or "none", data.get("filename", ""))
            stats["no_citekey"] += 1
            continue

        src = local_file_for(att, storage_dir)
        if src is None:
            logger.warning("  no local file: %s.pdf (att %s, sharing is off — "
                           "nothing to copy)", citekey, att_key)
            stats["missing_local"] += 1
            continue

        dest_name = f"{citekey}.pdf"
        dest = corpus_dir / dest_name

        prior = claimed.get(dest_name)
        if prior and prior != att_key:
            logger.warning("  collision: %s claimed by att %s and att %s — "
                           "skipping second", dest_name, prior, att_key)
            stats["collision"] += 1
            continue
        claimed[dest_name] = att_key

        if dest.exists() and not force:
            stats["skipped_exists"] += 1
            continue

        if dry_run:
            logger.info("  [dry-run] would copy %s -> %s", src.name, dest_name)
            stats["exported"] += 1
            continue

        try:
            shutil.copy2(src, dest)
            stats["exported"] += 1
        except OSError as e:
            logger.error("  copy failed for %s: %s", dest_name, e)
            stats["failed"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be exported; write nothing")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process at most N attachments (0 = all)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite corpus files that already exist")
    parser.add_argument("--storage-dir", type=Path, default=DEFAULT_STORAGE_DIR,
                        help=f"Zotero local storage dir (default: {DEFAULT_STORAGE_DIR})")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.storage_dir.is_dir():
        parser.error(f"storage dir not found: {args.storage_dir}")

    corpus = pdf_corpus_dir()
    logger.info("Source storage : %s", args.storage_dir)
    logger.info("Corpus target  : %s", corpus)
    if args.dry_run:
        logger.info("DRY RUN — no files will be written")

    stats = export_group_pdfs(
        storage_dir=args.storage_dir,
        corpus_dir=corpus,
        dry_run=args.dry_run,
        limit=args.limit,
        force=args.force,
    )

    print(f"\n{'='*52}")
    print(f"  Group PDF export {'(dry run)' if args.dry_run else ''}")
    print(f"  Corpus: {corpus}")
    print(f"{'='*52}")
    print(f"  Exported / would export : {stats['exported']:5d}")
    print(f"  Already in corpus       : {stats['skipped_exists']:5d}")
    print(f"  No citation key         : {stats['no_citekey']:5d}")
    print(f"  No local file (skipped) : {stats['missing_local']:5d}")
    print(f"  Citekey collisions      : {stats['collision']:5d}")
    print(f"  Copy failures           : {stats['failed']:5d}")
    print(f"{'='*52}\n")


if __name__ == "__main__":
    main()
