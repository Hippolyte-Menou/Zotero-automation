#!/usr/bin/env python3
"""Deduplicate items in the Zotero group library.

Clusters library items that share a DOI or PMID using Union-Find,
merges metadata into the most complete copy (canonical), reparents
child attachments, and deletes duplicates (sent to Zotero trash).

Dry-run by default -- review the JSON report before using --apply.

Usage:
    python dedup_library.py                   # dry-run, full report
    python dedup_library.py --apply           # execute merges
    python dedup_library.py --limit 10        # first 10 clusters only
    python dedup_library.py --apply --limit 5 # apply first 5 clusters
"""

import argparse
import copy
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict

import httpx
from pyzotero import errors as zotero_exceptions

from genebot.zotero_client import ZoteroGroupClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Union-Find
# ---------------------------------------------------------------


class _UnionFind:
    """Disjoint-set with path compression and union by rank."""

    def __init__(self):
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1


# ---------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------


_RE_VALID_DOI = re.compile(r"^10\.\d{4,}/\S+")


def _is_valid_doi(doi: str) -> bool:
    """Reject truncated DOIs that are just a publisher prefix (e.g. '10.1002/')."""
    return bool(doi and _RE_VALID_DOI.match(doi))


def _cluster_items(items: list[dict]) -> dict[str, list[dict]]:
    """Group items into duplicate clusters via shared DOI or PMID.

    Returns {root_key: [item_dicts]} for clusters with 2+ members.
    """
    uf = _UnionFind()
    doi_map: dict[str, list[str]] = defaultdict(list)
    pmid_map: dict[str, list[str]] = defaultdict(list)
    key_to_item: dict[str, dict] = {}

    for item in items:
        data = item.get("data", {})
        key = data.get("key", "")
        if not key:
            continue
        key_to_item[key] = item
        uf.find(key)

        doi = ZoteroGroupClient._extract_doi_from_data(data)
        pmid = ZoteroGroupClient._extract_pmid_from_data(data)

        if _is_valid_doi(doi):
            doi_map[doi].append(key)
        if pmid:
            pmid_map[pmid].append(key)

    for keys in doi_map.values():
        for k in keys[1:]:
            uf.union(keys[0], k)
    for keys in pmid_map.values():
        for k in keys[1:]:
            uf.union(keys[0], k)

    clusters: dict[str, list[dict]] = defaultdict(list)
    for key, item in key_to_item.items():
        clusters[uf.find(key)].append(item)

    return {r: members for r, members in clusters.items() if len(members) > 1}


# ---------------------------------------------------------------
# Scoring / canonical selection
# ---------------------------------------------------------------


def _score(item: dict) -> int:
    """Higher = more complete metadata (better canonical candidate)."""
    d = item.get("data", {})
    s = 0
    if ZoteroGroupClient._extract_doi_from_data(d):
        s += 10
    if ZoteroGroupClient._extract_pmid_from_data(d):
        s += 10
    if d.get("abstractNote", "").strip():
        s += 5
    if d.get("title", "").strip():
        s += 5
    if d.get("publicationTitle", "").strip():
        s += 3
    if d.get("date", "").strip():
        s += 2
    if d.get("creators"):
        s += 3
    for f in ("volume", "issue", "pages", "journalAbbreviation"):
        if d.get(f, "").strip():
            s += 1
    s += len(d.get("tags", []))
    s += 2 * len(d.get("collections", []))
    return s


def _pick_canonical(cluster: list[dict]) -> dict:
    return max(cluster, key=_score)


# ---------------------------------------------------------------
# Merging
# ---------------------------------------------------------------

_BACKFILL_FIELDS = (
    "publicationTitle",
    "journalAbbreviation",
    "volume",
    "issue",
    "pages",
    "date",
    "language",
    "ISSN",
    "accessDate",
)


def _merge_metadata(canonical: dict, duplicates: list[dict]) -> list[str]:
    """Merge fields from *duplicates* into *canonical* (in place).

    Returns a list of human-readable change descriptions.
    """
    data = canonical["data"]
    changes: list[str] = []

    canon_doi = ZoteroGroupClient._extract_doi_from_data(data)
    canon_pmid = ZoteroGroupClient._extract_pmid_from_data(data)

    for dup in duplicates:
        dd = dup["data"]
        dup_key = dd.get("key", "?")

        # -- DOI backfill -------------------------------------------
        if not canon_doi:
            dup_doi = ZoteroGroupClient._extract_doi_from_data(dd)
            if dup_doi:
                data["DOI"] = dup_doi
                canon_doi = dup_doi
                changes.append(f"DOI backfilled from {dup_key}: {dup_doi}")

        # -- PMID backfill ------------------------------------------
        if not canon_pmid:
            dup_pmid = ZoteroGroupClient._extract_pmid_from_data(dd)
            if dup_pmid:
                extra = data.get("extra", "")
                data["extra"] = (
                    f"PMID: {dup_pmid}\n{extra}" if extra else f"PMID: {dup_pmid}"
                )
                canon_pmid = dup_pmid
                changes.append(f"PMID backfilled from {dup_key}: {dup_pmid}")

        # -- URL backfill -------------------------------------------
        if not data.get("url", "").strip():
            dup_url = dd.get("url", "").strip()
            if dup_url:
                data["url"] = dup_url
                changes.append(f"URL backfilled from {dup_key}")

        # -- Abstract (keep longest) --------------------------------
        if len(dd.get("abstractNote", "")) > len(data.get("abstractNote", "")):
            data["abstractNote"] = dd["abstractNote"]
            changes.append(f"Abstract replaced (longer version from {dup_key})")

        # -- Simple field backfill ----------------------------------
        for field in _BACKFILL_FIELDS:
            if not data.get(field, "").strip() and dd.get(field, "").strip():
                data[field] = dd[field]
                changes.append(f"{field} backfilled from {dup_key}")

        # -- Creators backfill --------------------------------------
        if not data.get("creators") and dd.get("creators"):
            data["creators"] = dd["creators"]
            changes.append(f"Creators backfilled from {dup_key}")

        # -- Tags union ---------------------------------------------
        existing_tags = {t["tag"] for t in data.get("tags", [])}
        added_tags = []
        for t in dd.get("tags", []):
            if t["tag"] not in existing_tags:
                data.setdefault("tags", []).append(t)
                existing_tags.add(t["tag"])
                added_tags.append(t["tag"])
        if added_tags:
            changes.append(
                f"Tags added from {dup_key}: {', '.join(added_tags[:5])}"
                + (f" (+{len(added_tags) - 5} more)" if len(added_tags) > 5 else "")
            )

        # -- Collections union --------------------------------------
        existing_colls = set(data.get("collections", []))
        added_colls = []
        for c in dd.get("collections", []):
            if c not in existing_colls:
                data.setdefault("collections", []).append(c)
                existing_colls.add(c)
                added_colls.append(c)
        if added_colls:
            changes.append(f"Collections added from {dup_key}: {', '.join(added_colls)}")

        # -- Relations union ----------------------------------------
        existing_rels = data.get("relations", {}).get("dc:relation", [])
        if isinstance(existing_rels, str):
            existing_rels = [existing_rels]
        dup_rels = dd.get("relations", {}).get("dc:relation", [])
        if isinstance(dup_rels, str):
            dup_rels = [dup_rels]
        merged = list(set(existing_rels) | set(dup_rels))
        if len(merged) > len(existing_rels):
            data.setdefault("relations", {})["dc:relation"] = merged
            changes.append(f"Relations merged from {dup_key} (+{len(merged) - len(existing_rels)})")

    # Ensure PubMed URL if we have PMID but no URL
    if canon_pmid and not data.get("url", "").strip():
        data["url"] = f"https://pubmed.ncbi.nlm.nih.gov/{canon_pmid}/"
        changes.append("PubMed URL set from PMID")

    return changes


# ---------------------------------------------------------------
# Cluster diagnostics
# ---------------------------------------------------------------


def _cluster_match_reasons(cluster: list[dict]) -> list[str]:
    """Return why items in this cluster were grouped."""
    dois: dict[str, int] = defaultdict(int)
    pmids: dict[str, int] = defaultdict(int)
    for item in cluster:
        d = item["data"]
        doi = ZoteroGroupClient._extract_doi_from_data(d)
        pmid = ZoteroGroupClient._extract_pmid_from_data(d)
        if doi:
            dois[doi] += 1
        if pmid:
            pmids[pmid] += 1

    reasons = []
    for doi, count in dois.items():
        if count > 1:
            reasons.append(f"shared DOI: {doi}")
    for pmid, count in pmids.items():
        if count > 1:
            reasons.append(f"shared PMID: {pmid}")
    return reasons or ["transitive match (DOI<->PMID chain)"]


def _cluster_warnings(cluster: list[dict]) -> list[str]:
    """Flag potential data-quality issues in a cluster."""
    warnings = []
    dois = {ZoteroGroupClient._extract_doi_from_data(i["data"]) for i in cluster} - {""}
    pmids = {ZoteroGroupClient._extract_pmid_from_data(i["data"]) for i in cluster} - {""}
    if len(dois) > 1:
        warnings.append(f"Conflicting DOIs: {', '.join(sorted(dois))}")
    if len(pmids) > 1:
        warnings.append(f"Conflicting PMIDs: {', '.join(sorted(pmids))}")
    return warnings


# ---------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.ReadTimeout)):
        return True
    if isinstance(exc, zotero_exceptions.HTTPError):
        msg = str(exc)
        if "Code: 5" in msg or "Code: 409" in msg:
            return True
        resp = getattr(exc, "response", None)
        if resp is not None and hasattr(resp, "status_code"):
            return 500 <= resp.status_code < 600
    return False


def _retry_api(fn, label: str, retries: int = 3):
    """Call fn() with retries on transient Zotero errors."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt < retries - 1 and _is_retryable(e):
                wait = 3 * (attempt + 1)
                logger.warning(f"{label}: {e} (attempt {attempt + 1}/{retries}, retry in {wait}s)")
                time.sleep(wait)
            else:
                raise


# ---------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------


def _item_summary(item: dict) -> dict:
    """Compact summary for the JSON report."""
    d = item["data"]
    return {
        "key": d.get("key", ""),
        "title": (d.get("title", "") or "")[:120],
        "doi": ZoteroGroupClient._extract_doi_from_data(d),
        "pmid": ZoteroGroupClient._extract_pmid_from_data(d),
        "collections": d.get("collections", []),
        "tags_count": len(d.get("tags", [])),
        "score": _score(item),
    }


# ---------------------------------------------------------------
# Main dedup routine
# ---------------------------------------------------------------


def run_dedup(
    client: ZoteroGroupClient,
    *,
    apply: bool,
    limit: int | None,
    report_path: str,
    skip_warnings: bool = True,
):
    # 1. Fetch all non-attachment items
    logger.info("Fetching all library items...")
    items = client._paginate_with_retry(
        client.zot.items, "library items (dedup)", itemType="-attachment"
    )
    if not items:
        logger.error("No items fetched (empty library or API error)")
        return

    # Identifier coverage stats
    n_doi = n_pmid = n_both = n_neither = 0
    for item in items:
        d = item["data"]
        has_doi = bool(ZoteroGroupClient._extract_doi_from_data(d))
        has_pmid = bool(ZoteroGroupClient._extract_pmid_from_data(d))
        if has_doi and has_pmid:
            n_both += 1
        elif has_doi:
            n_doi += 1
        elif has_pmid:
            n_pmid += 1
        else:
            n_neither += 1

    logger.info(
        f"Fetched {len(items)} items: "
        f"{n_both} both DOI+PMID, {n_doi} DOI-only, "
        f"{n_pmid} PMID-only, {n_neither} no identifiers"
    )

    # 2. Cluster duplicates
    clusters = _cluster_items(items)
    total_dupes = sum(len(c) - 1 for c in clusters.values())
    logger.info(f"Found {len(clusters)} duplicate clusters ({total_dupes} items to merge)")

    if not clusters:
        logger.info("No duplicates found.")
        return

    # Largest clusters first for easier review
    sorted_clusters = sorted(clusters.values(), key=len, reverse=True)

    # Filter out clusters with conflicting identifiers (likely false positives)
    skipped_warning_clusters: list[dict] = []
    if skip_warnings:
        clean, warned = [], []
        for cluster in sorted_clusters:
            if _cluster_warnings(cluster):
                warned.append(cluster)
            else:
                clean.append(cluster)
        if warned:
            logger.info(
                f"Skipping {len(warned)} clusters with conflicting identifiers "
                f"(--skip-warnings). These need manual review."
            )
            skipped_warning_clusters = warned
        sorted_clusters = clean

    if limit:
        sorted_clusters = sorted_clusters[:limit]
        logger.info(f"Processing first {limit} clusters (--limit)")

    # 3. Process each cluster
    report = {
        "mode": "applied" if apply else "dry-run",
        "library_size": len(items),
        "identifier_coverage": {
            "both_doi_pmid": n_both,
            "doi_only": n_doi,
            "pmid_only": n_pmid,
            "neither": n_neither,
        },
        "clusters_found": len(clusters),
        "clusters_skipped_warnings": len(skipped_warning_clusters),
        "clusters_processed": len(sorted_clusters),
        "duplicates_total": total_dupes,
        "clusters": [],
    }
    stats = {"updated": 0, "reparented": 0, "trashed": 0, "errors": 0}

    for idx, cluster in enumerate(sorted_clusters, 1):
        canonical = _pick_canonical(cluster)
        canon_key = canonical["data"]["key"]
        duplicates = [i for i in cluster if i["data"]["key"] != canon_key]

        match_reasons = _cluster_match_reasons(cluster)
        warnings = _cluster_warnings(cluster)

        cluster_report = {
            "index": idx,
            "size": len(cluster),
            "match_reasons": match_reasons,
            "warnings": warnings,
            "canonical": _item_summary(canonical),
            "duplicates": [_item_summary(d) for d in duplicates],
            "changes": [],
            "status": "dry-run",
        }

        if warnings:
            for w in warnings:
                logger.warning(f"Cluster {idx}: {w}")

        if apply:
            try:
                # -- Merge metadata into canonical ----------------------
                changes = _merge_metadata(canonical, duplicates)
                cluster_report["changes"] = changes

                if changes:
                    _retry_api(
                        lambda: client.zot.update_item(canonical),
                        f"Cluster {idx}: update canonical {canon_key}",
                    )
                    stats["updated"] += 1
                    logger.info(
                        f"[{idx}/{len(sorted_clusters)}] Updated {canon_key} "
                        f"({len(changes)} changes)"
                    )
                    time.sleep(client.delay)

                # -- Reparent child items (PDFs, notes) -----------------
                for dup in duplicates:
                    dup_key = dup["data"]["key"]
                    try:
                        children = client.zot.children(dup_key)
                    except Exception as e:
                        logger.warning(f"  Could not fetch children of {dup_key}: {e}")
                        children = []

                    for child in children:
                        child_key = child["data"].get("key", "?")
                        child["data"]["parentItem"] = canon_key
                        try:
                            _retry_api(
                                lambda c=child: client.zot.update_item(c),
                                f"Reparent {child_key} -> {canon_key}",
                            )
                            stats["reparented"] += 1
                            logger.info(f"  Reparented {child_key} -> {canon_key}")
                        except Exception as e:
                            logger.error(f"  Failed to reparent {child_key}: {e}")
                            stats["errors"] += 1
                        time.sleep(client.delay)

                # -- Delete duplicates (goes to Zotero trash) -----------
                for dup in duplicates:
                    dup_key = dup["data"]["key"]
                    try:
                        _retry_api(
                            lambda d=dup: client.zot.delete_item(d),
                            f"Delete {dup_key}",
                        )
                        stats["trashed"] += 1
                        logger.info(f"  Deleted duplicate {dup_key}")
                    except Exception as e:
                        logger.error(f"  Failed to delete {dup_key}: {e}")
                        stats["errors"] += 1
                    time.sleep(client.delay)

                cluster_report["status"] = "applied"

            except Exception as e:
                logger.error(f"Cluster {idx} failed: {e}")
                cluster_report["status"] = f"error: {e}"
                stats["errors"] += 1
        else:
            # Dry-run: simulate merge on a copy to show what would change
            canon_copy = copy.deepcopy(canonical)
            changes = _merge_metadata(canon_copy, duplicates)
            cluster_report["changes"] = changes

        report["clusters"].append(cluster_report)

    report["stats"] = stats

    # 4. Save report
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # 5. Console summary
    print(f"\n{'=' * 60}")
    print(f"  Dedup {'APPLIED' if apply else 'DRY RUN'}")
    print(f"{'=' * 60}")
    print(f"  Library items:      {len(items)}")
    print(f"  Identifier coverage:")
    print(f"    DOI + PMID:       {n_both}")
    print(f"    DOI only:         {n_doi}")
    print(f"    PMID only:        {n_pmid}")
    print(f"    No identifiers:   {n_neither}")
    print(f"  Duplicate clusters: {len(clusters)} ({total_dupes} duplicates)")
    if skipped_warning_clusters:
        print(f"  Skipped (warnings): {len(skipped_warning_clusters)}")
    print(f"  Clusters processed: {len(sorted_clusters)}")
    if apply:
        print(f"  Canonicals updated: {stats['updated']}")
        print(f"  Attachments moved:  {stats['reparented']}")
        print(f"  Duplicates deleted: {stats['trashed']}")
        print(f"  Errors:             {stats['errors']}")
    print(f"  Report:             {report_path}")
    if not apply:
        print(f"\n  Review the report, then re-run with --apply to execute.")
    print(f"{'=' * 60}\n")


# ---------------------------------------------------------------
# CLI
# ---------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Deduplicate Zotero group library by DOI / PMID",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute merges and delete duplicates (default: dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N duplicate clusters",
    )
    parser.add_argument(
        "--report",
        default="dedup_report.json",
        help="Output report path (default: dedup_report.json)",
    )
    parser.add_argument(
        "--include-warnings",
        action="store_true",
        help="Include clusters with conflicting DOIs/PMIDs (skipped by default)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    group_id = "6432168"
    key_file = os.path.join(os.path.dirname(__file__), ".zotero-api-key")
    try:
        with open(key_file) as f:
            api_key = f.read().strip()
    except FileNotFoundError:
        print(f"Error: {key_file} not found", file=sys.stderr)
        sys.exit(1)
    if not api_key:
        print(f"Error: {key_file} is empty", file=sys.stderr)
        sys.exit(1)

    client = ZoteroGroupClient(group_id, api_key)
    run_dedup(
        client,
        apply=args.apply,
        limit=args.limit,
        report_path=args.report,
        skip_warnings=not args.include_warnings,
    )


if __name__ == "__main__":
    main()
