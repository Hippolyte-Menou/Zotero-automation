#!/usr/bin/env python3
"""Inverse Bot -- flag low-centrality Zotero library papers.

For each paper in the Zotero group library, compute its internal graph
centrality: how many other library papers it is connected to via:
  - backward links  (it cites a library paper)
  - forward links   (a library paper cites it)
  - bibliographic coupling (shares a reference with a library paper)

Papers with centrality = 0 (orphans -- zero connections to any other
library paper) are flagged and written to site/data/flagged_papers.json
for review in the dashboard "Review" tab.

Reference lists are fetched from OpenAlex and cached incrementally in
data/citation_cache.json under the 'inverse_bot' section so subsequent
runs only fetch data for new papers.

Usage:
    export ZOTERO_API_KEY="xxx"
    export ZOTERO_GROUP_ID="123456"
    python inverse_bot.py

Optional:
    export OPENALEX_API_KEY="xxx"   # higher rate limit
"""

import os
import sys
import json
import logging
import datetime
from collections import defaultdict

from genebot.openalex import OpenAlexClient
from genebot.zotero_client import ZoteroGroupClient

logger = logging.getLogger("inverse_bot")

FLAGGED_PAPERS_PATH = "site/data/flagged_papers.json"
WHITELIST_PATH = "data/inverse_bot_whitelist.json"
CACHE_PATH = "data/citation_cache.json"
LOGS_DIR = "logs"
DEFAULT_THRESHOLD_K = 0  # flag papers with centrality <= this value


# ------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------

def setup_logging() -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOGS_DIR, f"inverse_bot_{timestamp}.log")
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


# ------------------------------------------------------------------
# Whitelist (dismissed papers)
# ------------------------------------------------------------------

def load_whitelist(path: str) -> set[str]:
    """Load dismissed PMIDs from whitelist file. Returns empty set if missing."""
    if not os.path.isfile(path):
        logger.info("No whitelist file found -- no papers will be skipped")
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        dismissed = {
            entry["pmid"]
            for entry in data.get("dismissed", [])
            if entry.get("pmid")
        }
        logger.info(f"Loaded {len(dismissed)} dismissed PMIDs from whitelist")
        return dismissed
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load whitelist: {e}")
        return set()


# ------------------------------------------------------------------
# Cache
# ------------------------------------------------------------------

def load_cache(path: str) -> dict:
    """Load citation cache. Returns empty dict if missing or unreadable."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load citation cache: {e}")
        return {}


def save_cache(cache: dict, path: str) -> None:
    """Persist updated citation cache."""
    cache["last_run_date"] = datetime.date.today().isoformat()
    cache.setdefault("version", 1)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    inv_count = len(cache.get("inverse_bot", {}))
    logger.info(
        f"Saved citation cache ({inv_count} inverse_bot entries) -> {path}"
    )


# ------------------------------------------------------------------
# OpenAlex resolution
# ------------------------------------------------------------------

def resolve_library_to_openalex(
    items: list[dict],
    openalex: OpenAlexClient,
    cache: dict,
) -> tuple[dict[str, dict], int, int, int]:
    """Resolve library items to OpenAlex work data (including referenced_works).

    Uses cache["inverse_bot"] for already-fetched items.
    New fetches are stored back in the cache.

    Cache is keyed by OpenAlex work ID.  Each entry holds:
      {pmid, doi, referenced_works, cited_by_count, fetched_at}

    Returns:
      - work_map: {lookup_key -> {oa_id, refs: set, cited_by_count}}
        lookup_key is pmid when available, else "doi:<doi>"
      - n_cached:       papers loaded from cache
      - n_fetched:      papers newly fetched from OpenAlex
      - n_unresolvable: papers not found on OpenAlex
    """
    inv_cache: dict[str, dict] = cache.setdefault("inverse_bot", {})

    # Build reverse lookup: pmid -> oa_id  and  doi -> oa_id  from cache
    cached_by_pmid: dict[str, str] = {}
    cached_by_doi: dict[str, str] = {}
    for oa_id, cdata in inv_cache.items():
        if cdata.get("pmid"):
            cached_by_pmid[cdata["pmid"]] = oa_id
        if cdata.get("doi"):
            cached_by_doi[cdata["doi"]] = oa_id

    pmid_items = [it for it in items if it.get("pmid")]
    doi_only_items = [it for it in items if not it.get("pmid") and it.get("doi")]

    work_map: dict[str, dict] = {}
    n_cached = 0
    n_fetched = 0
    n_unresolvable = 0

    # ---- PMID-keyed items ----
    pmids_to_fetch: list[str] = []
    for it in pmid_items:
        pmid = it["pmid"]
        if pmid in cached_by_pmid:
            oa_id = cached_by_pmid[pmid]
            cdata = inv_cache[oa_id]
            work_map[pmid] = {
                "oa_id": oa_id,
                "refs": set(cdata.get("referenced_works", [])),
                "cited_by_count": cdata.get("cited_by_count", 0),
            }
            n_cached += 1
        else:
            pmids_to_fetch.append(pmid)

    if pmids_to_fetch:
        logger.info(
            f"Fetching OpenAlex reference data for {len(pmids_to_fetch)} papers (by PMID)..."
        )
        works = openalex.fetch_works_by_pmids(pmids_to_fetch)
        fetched_pmids: set[str] = set()
        for work in works:
            pmid = openalex.extract_pmid(work)
            if not pmid:
                continue
            oa_id = work.get("id", "")
            if not oa_id:
                continue
            refs = set(work.get("referenced_works", []))
            cited_by = work.get("cited_by_count", 0)
            doi_raw = (work.get("ids") or {}).get("doi", "")
            doi = (
                doi_raw.replace("https://doi.org/", "").lower()
                if doi_raw
                else ""
            )
            inv_cache[oa_id] = {
                "pmid": pmid,
                "doi": doi,
                "referenced_works": list(refs),
                "cited_by_count": cited_by,
                "fetched_at": datetime.date.today().isoformat(),
            }
            work_map[pmid] = {"oa_id": oa_id, "refs": refs, "cited_by_count": cited_by}
            fetched_pmids.add(pmid)
            n_fetched += 1

        for pmid in pmids_to_fetch:
            if pmid not in fetched_pmids:
                logger.warning(
                    f"Could not resolve PMID {pmid} on OpenAlex -- skipping"
                )
                n_unresolvable += 1

    # ---- DOI-only items ----
    dois_to_fetch: list[str] = []
    for it in doi_only_items:
        doi = it["doi"]
        if doi in cached_by_doi:
            oa_id = cached_by_doi[doi]
            cdata = inv_cache[oa_id]
            work_map[f"doi:{doi}"] = {
                "oa_id": oa_id,
                "refs": set(cdata.get("referenced_works", [])),
                "cited_by_count": cdata.get("cited_by_count", 0),
            }
            n_cached += 1
        else:
            dois_to_fetch.append(doi)

    if dois_to_fetch:
        logger.info(
            f"Fetching OpenAlex reference data for {len(dois_to_fetch)} papers (by DOI)..."
        )
        doi_works = openalex.fetch_works_by_dois(dois_to_fetch)
        fetched_dois: set[str] = set()
        for work in doi_works:
            oa_id = work.get("id", "")
            if not oa_id:
                continue
            ids = work.get("ids", {})
            raw_doi = ids.get("doi", "")
            if not raw_doi:
                continue
            norm_doi = raw_doi.replace("https://doi.org/", "").replace("http://doi.org/", "").lower()
            refs = set(work.get("referenced_works", []))
            cited_by = work.get("cited_by_count", 0)
            pmid = openalex.extract_pmid(work) or ""
            inv_cache[oa_id] = {
                "pmid": pmid,
                "doi": norm_doi,
                "referenced_works": list(refs),
                "cited_by_count": cited_by,
                "fetched_at": datetime.date.today().isoformat(),
            }
            lookup_key = pmid if pmid else f"doi:{norm_doi}"
            work_map[lookup_key] = {
                "oa_id": oa_id,
                "refs": refs,
                "cited_by_count": cited_by,
            }
            fetched_dois.add(norm_doi)
            n_fetched += 1

        for doi in dois_to_fetch:
            if doi not in fetched_dois:
                logger.warning(
                    f"Could not resolve DOI {doi} on OpenAlex -- skipping"
                )
                n_unresolvable += 1

    return work_map, n_cached, n_fetched, n_unresolvable


# ------------------------------------------------------------------
# Graph centrality
# ------------------------------------------------------------------

def compute_centralities(work_map: dict[str, dict]) -> dict[str, int]:
    """Compute internal graph centrality for each library paper.

    For paper P:
      - backward neighbors: refs of P that are other library papers
      - forward neighbors:  library papers whose refs contain P
      - bib-coupling neighbors: library papers sharing at least one ref with P

    centrality(P) = |union of all three neighbor sets| (excluding P itself)

    Returns {lookup_key -> centrality}.
    """
    library_oa_ids: set[str] = {w["oa_id"] for w in work_map.values()}

    # Inverted index: ref_id -> set of library OA IDs that list it in their refs
    inverted: dict[str, set[str]] = defaultdict(set)
    for work in work_map.values():
        for ref in work["refs"]:
            inverted[ref].add(work["oa_id"])

    centralities: dict[str, int] = {}
    for key, work in work_map.items():
        oa_id = work["oa_id"]
        refs = work["refs"]

        # backward: P cites another library paper
        backward = refs & library_oa_ids - {oa_id}

        # forward: another library paper cites P
        forward = inverted.get(oa_id, set()) - {oa_id}

        # bib coupling: library papers sharing a reference with P
        bib: set[str] = set()
        for ref in refs:
            bib.update(inverted.get(ref, set()))
        bib.discard(oa_id)

        centralities[key] = len(backward | forward | bib)

    return centralities


# ------------------------------------------------------------------
# Output
# ------------------------------------------------------------------

def build_flagged_articles(
    items: list[dict],
    work_map: dict[str, dict],
    centralities: dict[str, int],
    threshold_k: int,
) -> list[dict]:
    """Return list of article dicts for papers with centrality <= threshold_k."""
    pmid_to_item: dict[str, dict] = {
        it["pmid"]: it for it in items if it.get("pmid")
    }
    doi_to_item: dict[str, dict] = {
        it["doi"]: it
        for it in items
        if not it.get("pmid") and it.get("doi")
    }

    flagged: list[dict] = []
    for key, centrality in centralities.items():
        if centrality > threshold_k:
            continue

        if key.startswith("doi:"):
            it = doi_to_item.get(key[4:])
        else:
            it = pmid_to_item.get(key)

        if not it:
            continue

        work = work_map.get(key, {})
        flagged.append({
            "pmid": it.get("pmid", ""),
            "doi": it.get("doi", ""),
            "zotero_key": it.get("zotero_key", ""),
            "title": it.get("title", ""),
            "authors": it.get("authors", []),
            "journal": it.get("journal", ""),
            "year": it.get("year", ""),
            "abstract": it.get("abstract", ""),
            "cited_by_count": work.get("cited_by_count", 0),
            "centrality": centrality,
            "source_tags": it.get("source_tags", []),
            "category": it.get("category", ""),
            "subcollection": it.get("subcollection", ""),
        })

    flagged.sort(key=lambda a: a.get("title", "").lower())
    return flagged


def save_flagged_papers(
    flagged: list[dict],
    stats: dict,
    threshold_k: int,
    path: str,
) -> None:
    """Write site/data/flagged_papers.json."""
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Build hierarchy dict from flagged articles (same format as near_misses.json)
    hierarchy: dict[str, set] = {}
    for a in flagged:
        cats = [c.strip() for c in a.get("category", "").split(",") if c.strip()]
        subs = [s.strip() for s in a.get("subcollection", "").split(",") if s.strip()]
        for cat in cats:
            hierarchy.setdefault(cat, set()).update(subs)
    hierarchy_sorted = {k: sorted(v) for k, v in sorted(hierarchy.items())}

    output = {
        "generated_at": stats["generated_at"],
        "threshold_k": threshold_k,
        "total_evaluated": stats["total_evaluated"],
        "total_flagged": len(flagged),
        "total_dismissed": stats["total_dismissed"],
        "total_unresolvable": stats["total_unresolvable"],
        "hierarchy": hierarchy_sorted,
        "articles": flagged,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved {len(flagged)} flagged papers -> {path}")


def write_github_summary(stats: dict) -> None:
    """Write markdown summary to $GITHUB_STEP_SUMMARY (no-op outside Actions)."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## Inverse Bot Summary",
        "",
        f"**Date:** {stats['generated_at']}",
        "",
        "| Metric | Count |",
        "|--------|------:|",
        f"| Papers evaluated | {stats['total_evaluated']} |",
        f"| Flagged (centrality = 0) | {stats['total_flagged']} |",
        f"| Dismissed (whitelisted) | {stats['total_dismissed']} |",
        f"| Unresolvable (no OpenAlex match) | {stats['total_unresolvable']} |",
        f"| Loaded from cache | {stats['total_cached']} |",
        f"| Newly fetched | {stats['total_fetched']} |",
        "",
    ]
    if stats.get("openalex_failed_requests", 0) > 0:
        lines.append(
            f"> **{stats['openalex_failed_requests']} OpenAlex requests failed** "
            f"-- some papers may have been skipped."
        )
        lines.append("")
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        logger.warning(f"Could not write GitHub summary: {e}")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    setup_logging()
    logger.info("=== Inverse Bot starting ===")

    # --- Credentials ---
    api_key = os.environ.get("ZOTERO_API_KEY", "")
    group_id = os.environ.get("ZOTERO_GROUP_ID", "")
    oa_key = os.environ.get("OPENALEX_API_KEY")

    if not api_key or not group_id:
        logger.error("ZOTERO_API_KEY and ZOTERO_GROUP_ID must be set")
        sys.exit(1)

    threshold_k = DEFAULT_THRESHOLD_K

    # --- Load whitelist ---
    whitelist_pmids = load_whitelist(WHITELIST_PATH)

    # --- Fetch Zotero library ---
    zotero = ZoteroGroupClient(group_id, api_key)
    all_items = zotero.get_all_items_full()
    logger.info(f"Retrieved {len(all_items)} library items with identifiers")

    total_dismissed = sum(
        1 for it in all_items if it.get("pmid") in whitelist_pmids
    )
    items = [it for it in all_items if it.get("pmid") not in whitelist_pmids]
    logger.info(
        f"After whitelist filter: {len(items)} items to evaluate "
        f"({total_dismissed} dismissed)"
    )

    # --- Load citation cache ---
    cache = load_cache(CACHE_PATH)

    # --- Resolve OpenAlex IDs and reference lists ---
    openalex = OpenAlexClient(api_key=oa_key)
    work_map, n_cached, n_fetched, n_unresolvable = resolve_library_to_openalex(
        items, openalex, cache
    )
    logger.info(
        f"OpenAlex resolution: {n_cached} cached, {n_fetched} fetched, "
        f"{n_unresolvable} unresolvable"
    )

    # --- Compute centralities ---
    logger.info(
        f"Computing internal graph centrality for {len(work_map)} papers..."
    )
    centralities = compute_centralities(work_map)

    n_orphans = sum(1 for c in centralities.values() if c <= threshold_k)
    logger.info(
        f"Centrality computed: {n_orphans} orphans (centrality <= {threshold_k})"
    )

    stats = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "total_evaluated": len(work_map),
        "total_flagged": n_orphans,
        "total_dismissed": total_dismissed,
        "total_unresolvable": n_unresolvable,
        "total_cached": n_cached,
        "total_fetched": n_fetched,
        "openalex_failed_requests": openalex.failed_request_count,
    }

    # --- Build and save output ---
    flagged = build_flagged_articles(items, work_map, centralities, threshold_k)
    save_cache(cache, CACHE_PATH)
    save_flagged_papers(flagged, stats, threshold_k, FLAGGED_PAPERS_PATH)
    write_github_summary(stats)

    logger.info(
        f"=== Inverse Bot complete: {n_orphans} flagged out of "
        f"{len(work_map)} evaluated ==="
    )


if __name__ == "__main__":
    main()
