"""Repair category/subcollection pairings corrupted by the independent-CSV merge.

Before the pair-aware merge landed in ``RejectionLog``, the cumulative merge
deduplicated and sorted ``category`` and ``subcollection`` independently. For an
article near-missed under both a gene and a topic that desynchronised the two
parallel lists, e.g.

    category      = "2 - Embryology, 6 - Genes"
    subcollection = "ABCA4, Coat differentiation"

which downstream consumers zip positionally -- putting ABCA4 under
2 - Embryology in the dashboard hierarchy, and (via the rescue queue) creating a
real ABCA4 collection under that topic category in the Zotero group.

The pairing is not recoverable from the entry itself, but it is recoverable from
the configuration: every subcollection name belongs to exactly one category
(gene symbols to "6 - Genes", sub-topic collections to their category in
topics.yml). This script rewrites each entry's two fields from that mapping.
Subcollections that match no known collection are dropped, and the hierarchy /
stats blocks are rebuilt from the repaired entries.

Usage:
    python repair_near_miss_context.py site/data/near_misses.json [more.json ...]
    python repair_near_miss_context.py --apply site/data/near_misses.json

Dry-run by default; --apply rewrites the files in place (after a .bak copy).
Also accepts rescue_queue.json (a bare list of entries) and flagged_papers.json.
"""

import argparse
import json
import logging
import os
import shutil
import sys

import yaml

from genebot.rejection_log import RejectionLog

logger = logging.getLogger(__name__)

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
GENES_YML = os.path.join(REPO_DIR, "genes.yml")
TOPICS_YML = os.path.join(REPO_DIR, "topics.yml")


def build_sub_to_category(
    genes_path: str = GENES_YML, topics_path: str = TOPICS_YML
) -> dict[str, str]:
    """Map every known subcollection name to its parent category name."""
    mapping: dict[str, str] = {}

    with open(genes_path, encoding="utf-8") as f:
        genes_cfg = yaml.safe_load(f) or {}
    genes_parent = (genes_cfg.get("collections") or {}).get("genes_parent", "6 - Genes")
    for gene in genes_cfg.get("genes") or []:
        name = gene.get("collection") or gene.get("symbol")
        if name:
            mapping[name] = genes_parent

    with open(topics_path, encoding="utf-8") as f:
        topics_cfg = yaml.safe_load(f) or {}
    for category in topics_cfg.get("categories") or []:
        cat_name = category.get("name")
        if not cat_name:
            continue
        for sub in category.get("sub_topics") or []:
            name = sub.get("collection") or sub.get("name")
            if name:
                mapping[name] = cat_name

    return mapping


def learn_from_singletons(entries: list[dict]) -> dict[str, str]:
    """Infer subcollection -> category from entries that cannot be mispaired.

    An entry listing exactly one collection has nothing to desynchronise, so its
    pairing is trustworthy. This recovers names the YAML no longer knows about:
    genes since removed from genes.yml, and libraries whose collection names are
    translated (the Zotero group uses French names while topics.yml is English).
    Conflicts are resolved by majority vote.
    """
    votes: dict[str, dict[str, int]] = {}
    for entry in entries:
        cats = [c.strip() for c in (entry.get("category") or "").split(",") if c.strip()]
        subs = [s.strip() for s in (entry.get("subcollection") or "").split(",") if s.strip()]
        if len(cats) == 1 and len(subs) == 1:
            votes.setdefault(subs[0], {})
            votes[subs[0]][cats[0]] = votes[subs[0]].get(cats[0], 0) + 1
    return {
        sub: max(counts.items(), key=lambda kv: kv[1])[0]
        for sub, counts in votes.items()
    }


def repair_entry(entry: dict, sub_to_cat: dict[str, str]) -> tuple[bool, list[str]]:
    """Rewrite one entry's category/subcollection from the authoritative map.

    Subcollections with no known category keep whatever category they were
    positionally paired with -- dropping them would lose real data, and for an
    uncorrupted entry that pairing is already right. They are reported so the
    remaining uncertainty is visible.

    Returns (changed, unknown_subcollection_names).
    """
    cats = [c.strip() for c in (entry.get("category") or "").split(",") if c.strip()]
    subs = [s.strip() for s in (entry.get("subcollection") or "").split(",") if s.strip()]
    before = (entry.get("category") or "", entry.get("subcollection") or "")

    pairs: list[tuple[str, str]] = []
    unknown: list[str] = []
    for i, sub in enumerate(subs):
        cat = sub_to_cat.get(sub)
        if cat is None:
            unknown.append(sub)
            cat = cats[i] if i < len(cats) else ""
            if not cat:
                continue
        pairs.append((cat, sub))

    RejectionLog._write_context_pairs(entry, pairs)
    after = (entry.get("category") or "", entry.get("subcollection") or "")
    return before != after, unknown


def repair_file(path: str, sub_to_cat: dict[str, str], apply: bool) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and isinstance(data.get("articles"), list):
        entries = data["articles"]
    else:
        raise SystemExit(f"{path}: unrecognised shape (no 'articles' list)")

    # Names the YAML does not cover are recovered from the file's own
    # single-collection entries, which cannot have been mispaired.
    effective_map = {**learn_from_singletons(entries), **sub_to_cat}

    changed = 0
    unknown_names: dict[str, int] = {}
    for entry in entries:
        was_changed, unknown = repair_entry(entry, effective_map)
        if was_changed:
            changed += 1
        for name in unknown:
            unknown_names[name] = unknown_names.get(name, 0) + 1

    if isinstance(data, dict):
        if "hierarchy" in data:
            data["hierarchy"] = RejectionLog._build_hierarchy(entries)
        if "stats" in data:
            data["stats"] = RejectionLog._build_stats(entries)

    if apply:
        shutil.copyfile(path, path + ".bak")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    return {
        "path": path,
        "entries": len(entries),
        "changed": changed,
        "unknown": unknown_names,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="JSON files to repair")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="rewrite the files in place (a .bak copy is kept)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sub_to_cat = build_sub_to_category()
    logger.info(f"Loaded {len(sub_to_cat)} known subcollection -> category mappings")

    for path in args.paths:
        if not os.path.isfile(path):
            logger.warning(f"{path}: not found, skipping")
            continue
        result = repair_file(path, sub_to_cat, args.apply)
        verb = "repaired" if args.apply else "would repair"
        logger.info(
            f"{result['path']}: {result['entries']} entries, "
            f"{verb} {result['changed']}"
        )
        if result["unknown"]:
            total = sum(result["unknown"].values())
            names = sorted(result["unknown"])[:10]
            logger.info(
                f"  {total} reference(s) to subcollections of unknown category "
                f"kept as-is: {', '.join(names)}"
                + (" ..." if len(result["unknown"]) > 10 else "")
            )

    if not args.apply:
        logger.info("Dry run -- re-run with --apply to write the changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
