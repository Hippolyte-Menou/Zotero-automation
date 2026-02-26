#!/usr/bin/env python3
"""
Gene Literature Bot
OpenAlex search + citation network -> Zotero group library.

Reads gene list from genes.yml, credentials from environment variables.

Local usage:
    export ZOTERO_API_KEY="xxx"
    export ZOTERO_GROUP_ID="123456"
    python run.py                     # all genes from genes.yml
    python run.py CRB1 RHO            # specific genes only

GitHub Actions:
    Secrets are injected as env vars by the workflow.
    INPUT_GENES env var can override the gene list.
"""

import os
import re
import sys
import json
import logging
import datetime
import yaml

from genebot.hgnc import get_gene_aliases
from genebot.openalex import OpenAlexClient
from genebot.zotero_client import ZoteroGroupClient
from genebot.rejection_log import RejectionLog

logger = logging.getLogger("genebot")


def filter_records_by_text(
    records: list[dict],
    exclude_terms: list[str],
    rejection_log=None,
) -> list[dict]:
    """Filter records by text exclusion terms. Returns non-matching records."""
    if not exclude_terms:
        return records
    patterns = [
        (re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE), t)
        for t in exclude_terms
    ]
    filtered = []
    for r in records:
        text = f"{r.get('title', '')} {r.get('abstract', '')}"
        matched = None
        for pat, term in patterns:
            if pat.search(text):
                matched = term
                break
        if matched:
            if rejection_log:
                rejection_log.add_from_record(
                    r, reason="text_exclusion", matched_term=matched
                )
        else:
            filtered.append(r)
    return filtered


def load_genes_config(path: str = "genes.yml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_topics_config(path: str = "topics.yml") -> dict:
    """Load topic configuration. Returns empty dict if file not found."""
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"Topics config not found at {path}")
        return {}


def load_citation_cache(path: str = "data/citation_cache.json") -> dict | None:
    """Load citation cache from previous run. Returns None if not found."""
    if not os.path.isfile(path):
        logger.info("No citation cache found, will do full expansion")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        seed_count = len(cache.get("seeds", {}))
        if seed_count == 0:
            logger.info("Citation cache is empty, will do full expansion")
            return None
        logger.info(
            f"Loaded citation cache: {seed_count} seeds, "
            f"last run: {cache.get('last_run_date', 'unknown')}"
        )
        return cache
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load citation cache: {e}")
        return None


def save_citation_cache(cache: dict, path: str = "data/citation_cache.json") -> None:
    """Save citation cache for next run."""
    cache["last_run_date"] = datetime.date.today().isoformat()
    cache.setdefault("version", 1)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    seed_count = len(cache.get("seeds", {}))
    logger.info(f"Saved citation cache: {seed_count} seeds -> {path}")


def select_rotation_genes(
    all_genes: list[dict],
    citation_cache: dict,
) -> set[str]:
    """Select genes due for full forward expansion this week.

    Returns set of gene symbols. Picks len(all_genes)//4 genes
    sorted by oldest last_full_expanded date.
    """
    batch_size = max(1, len(all_genes) // 4)
    gene_dates = citation_cache.get("genes", {})
    ranked = sorted(
        all_genes,
        key=lambda g: gene_dates.get(g["symbol"], {}).get(
            "last_full_expanded", "2000-01-01"
        ),
    )
    return {g["symbol"] for g in ranked[:batch_size]}


def _get_subtopic_keywords(sub_topic: dict) -> list[str]:
    """Extract all search keywords from a sub_topic config.

    Handles two formats:
    - keywords: [...] (anatomy/embryo/physio/exams)
    - diseases: [{en_keywords: [...]}, ...] (pathologies)
    """
    keywords = list(sub_topic.get("keywords", []))
    for disease in sub_topic.get("diseases", []):
        keywords.extend(disease.get("en_keywords", []))
    return keywords


def _get_mention_terms(sub_topic: dict) -> list[str]:
    """Extract terms for citation expansion mention filtering.

    Uses clinical_scope if available (anatomy/physio -- broad terms like
    'retina', 'cornea'). Falls back to disease en_keywords, then keywords.
    """
    terms = list(sub_topic.get("clinical_scope", []))
    if not terms:
        for disease in sub_topic.get("diseases", []):
            terms.extend(disease.get("en_keywords", []))
    if not terms:
        terms = list(sub_topic.get("keywords", []))
    return terms


def _link_relations(
    zot: ZoteroGroupClient,
    openalex: OpenAlexClient,
    new_pmids: set[str],
    pmid_to_oa_refs: dict[str, list[str]],
    pmid_to_key: dict[str, str],
    gene_symbol: str,
) -> None:
    """For each newly uploaded paper, resolve referenced_works to Zotero keys
    and set bidirectional dc:relation links between items in the library.
    """
    all_oa_ids: set[str] = set()
    for pmid in new_pmids:
        all_oa_ids.update(pmid_to_oa_refs.get(pmid, []))

    if not all_oa_ids:
        return

    logger.info(
        f"{gene_symbol}: resolving {len(all_oa_ids)} OpenAlex reference IDs "
        f"for {len(new_pmids)} newly uploaded papers"
    )
    oa_id_to_pmid = openalex.resolve_openalex_ids_to_pmids(list(all_oa_ids))

    linked = 0
    for pmid in new_pmids:
        if pmid not in pmid_to_key:
            continue
        oa_refs = pmid_to_oa_refs.get(pmid, [])
        related_keys = [
            pmid_to_key[oa_id_to_pmid[oa_id]]
            for oa_id in oa_refs
            if oa_id in oa_id_to_pmid
            and oa_id_to_pmid[oa_id] in pmid_to_key
            and oa_id_to_pmid[oa_id] != pmid
        ]
        if related_keys:
            logger.info(
                f"{gene_symbol}: {pmid} -> {len(related_keys)} related items"
            )
            zot.add_relations(pmid_to_key[pmid], related_keys)
            linked += 1

    logger.info(f"{gene_symbol}: linked relations for {linked} newly uploaded papers")


def process_gene(
    gene_cfg: dict,
    default_excl_text: list[str],
    default_excl_mesh: list[str],
    genes_parent_key: str | None,
    zot: ZoteroGroupClient,
    existing_pmids: set[str],
    pmid_to_key: dict[str, str],
    openalex: OpenAlexClient,
    citation_cfg: dict,
    search_max_results: int = 25,
    recent_max_results: int = 10,
    rejection_log: RejectionLog | None = None,
    citation_cache: dict | None = None,
    force_full_expansion: bool = False,
) -> dict:
    """Process a single gene. Returns stats dict."""

    symbol = gene_cfg["symbol"]
    collection_name = gene_cfg.get("collection", symbol)
    text_excl = gene_cfg.get("exclude_text", default_excl_text)
    mesh_excl = gene_cfg.get("exclude_mesh", default_excl_mesh)
    gene_tags = gene_cfg.get("tags", [])

    # Track pmid -> OpenAlex referenced_works for relation linking
    pmid_to_oa_refs: dict[str, list[str]] = {}
    # Track PMIDs uploaded in this run (for relation linking)
    new_pmids: set[str] = set()

    logger.info(f"{'=' * 60}")
    logger.info(f"Processing: {symbol}")
    logger.info(f"{'=' * 60}")

    if rejection_log:
        rejection_log.set_context(subcollection=symbol, category="6 - Genes")

    # 1. Gene aliases from HGNC
    aliases = get_gene_aliases(symbol)
    blocked = set(gene_cfg.get("blocked_aliases", []))
    if blocked:
        removed = aliases & blocked
        if removed:
            logger.info(f"{symbol}: blocking HGNC aliases: {sorted(removed)}")
            aliases -= blocked
    search_terms = sorted(aliases)

    # 2. Get/create Zotero collection (needed before fetching existing papers)
    if genes_parent_key:
        collection_key = zot.get_or_create_collection(
            collection_name, parent_key=genes_parent_key
        )
    else:
        collection_key = zot.get_or_create_collection(collection_name)

    # 3. Check existing papers in this gene's collection
    collection_pmids = zot.get_collection_pmids(collection_key)
    search_stats = {"added": 0, "failed": 0}

    if collection_pmids:
        # Gene already has papers in Zotero -- use them as seeds directly.
        # Skip OpenAlex search to avoid redundant work.
        logger.info(
            f"{symbol}: {len(collection_pmids)} existing papers in collection, "
            f"using as citation seeds (skipping OpenAlex search)"
        )
        seed_works = openalex.fetch_works_by_pmids(collection_pmids)
        all_records = seed_works
        new_records = []
        library_size = len(collection_pmids)
    else:
        # No existing papers -- run OpenAlex search to bootstrap the collection.
        logger.info(f"{symbol}: collection empty, running OpenAlex search")
        seed_works = openalex.search_gene(
            search_terms,
            exclude_terms=text_excl,
            max_results=search_max_results,
            disease_keywords=gene_tags or None,
            rejection_log=rejection_log,
        )

        if not seed_works:
            logger.info(f"No results for {symbol}")
            return {
                "symbol": symbol, "found": 0, "new": 0,
                "added": 0, "failed": 0,
                "cit_candidates": 0, "cit_added": 0, "recent_added": 0
            }

        # Apply MeSH exclusion on raw works before converting to records
        if mesh_excl:
            seed_works = openalex.filter_by_mesh(
                seed_works, mesh_excl, rejection_log=rejection_log
            )

        # Preserve referenced_works before flattening
        for w in seed_works:
            pmid = OpenAlexClient.extract_pmid(w)
            if pmid:
                pmid_to_oa_refs[pmid] = w.get("referenced_works", [])

        all_records = [OpenAlexClient.work_to_record(w) for w in seed_works]
        new_records = [
            r for r in all_records
            if r.get("pmid") and r["pmid"] not in existing_pmids
        ]
        logger.info(
            f"{symbol}: {len(all_records)} from OpenAlex search, "
            f"{len(new_records)} new ({len(all_records) - len(new_records)} already in library)"
        )

        if new_records:
            search_stats = zot.add_papers(
                new_records,
                collection_key=collection_key,
                gene_symbol=symbol,
                extra_tags=gene_tags,
                source_tag="source:search",
            )
            pmid_to_key.update(search_stats.get("pmid_to_key", {}))
            for r in new_records:
                existing_pmids.add(r["pmid"])
                new_pmids.add(r["pmid"])

        library_size = len(collection_pmids) + len(new_records)

    # 4. Citation network expansion (multi-hop, gene-filtered, bib coupling)
    max_seeds = citation_cfg.get("max_seed_papers", 100)
    min_co = citation_cfg.get("min_co_citations", 1)
    max_min_co = citation_cfg.get("max_min_co", 6)
    max_hops = citation_cfg.get("max_hops", 2)
    hop2_top_n = citation_cfg.get("hop2_top_n", 10)

    # force_full_expansion: pass None cache so _expand_one_hop does full
    # forward expansion (no skip-gate, no since_date filter)
    effective_cache = None if force_full_expansion else citation_cache
    candidates = openalex.expand_citations(
        seed_works=seed_works,
        existing_pmids=existing_pmids,
        library_size=library_size,
        max_seeds=max_seeds,
        min_co_citations=min_co,
        max_min_co=max_min_co,
        mention_terms=search_terms,
        max_hops=max_hops,
        hop2_top_n=hop2_top_n,
        exclude_mesh=mesh_excl or None,
        rejection_log=rejection_log,
        citation_cache=effective_cache,
    )

    # Update gene rotation date in the real cache
    if force_full_expansion and citation_cache is not None:
        citation_cache.setdefault("genes", {})[symbol] = {
            "last_full_expanded": datetime.date.today().isoformat()
        }

    cit_added = 0
    if candidates:
        # Candidates already have full metadata attached (key 'work')
        # Preserve referenced_works before flattening
        for c in candidates:
            w = c.get("work", {})
            pmid = OpenAlexClient.extract_pmid(w)
            if pmid:
                pmid_to_oa_refs.setdefault(pmid, w.get("referenced_works", []))

        candidate_records = [
            OpenAlexClient.work_to_record(c["work"]) for c in candidates
        ]

        # Text filter + dedup
        candidate_records = filter_records_by_text(candidate_records, text_excl, rejection_log)

        candidate_records = [
            r for r in candidate_records
            if r.get("pmid") and r["pmid"] not in existing_pmids
        ]

        if candidate_records:
            cit_stats = zot.add_papers(
                candidate_records,
                collection_key=collection_key,
                gene_symbol=symbol,
                extra_tags=gene_tags,
                source_tag="source:citation",
            )
            cit_added = cit_stats["added"]
            pmid_to_key.update(cit_stats.get("pmid_to_key", {}))
            for r in candidate_records:
                existing_pmids.add(r["pmid"])
                new_pmids.add(r["pmid"])

    # 5. Recent papers pass -- bypass citation threshold for current-year papers
    recent_works = openalex.search_gene_recent(
        search_terms,
        disease_keywords=gene_tags or None,
        max_results=recent_max_results,
    )
    recent_added = 0
    if recent_works:
        # MeSH exclusion on raw works before converting to records
        if mesh_excl:
            recent_works = openalex.filter_by_mesh(
                recent_works, mesh_excl, rejection_log=rejection_log
            )

        # Preserve referenced_works before flattening
        for w in recent_works:
            pmid = OpenAlexClient.extract_pmid(w)
            if pmid:
                pmid_to_oa_refs.setdefault(pmid, w.get("referenced_works", []))

        recent_records = [
            OpenAlexClient.work_to_record(w) for w in recent_works
        ]

        # Text filter
        recent_records = filter_records_by_text(recent_records, text_excl, rejection_log)

        # Dedup
        recent_records = [
            r for r in recent_records
            if r.get("pmid") and r["pmid"] not in existing_pmids
        ]

        logger.info(
            f"{symbol}: recent-papers pass: {len(recent_works)} found, "
            f"{len(recent_records)} new after dedup"
        )

        if recent_records:
            rec_stats = zot.add_papers(
                recent_records,
                collection_key=collection_key,
                gene_symbol=symbol,
                extra_tags=gene_tags,
                source_tag="source:recent",
            )
            recent_added = rec_stats["added"]
            pmid_to_key.update(rec_stats.get("pmid_to_key", {}))
            for r in recent_records:
                existing_pmids.add(r["pmid"])
                new_pmids.add(r["pmid"])

    # Link relations for all newly uploaded papers
    if new_pmids:
        _link_relations(
            zot=zot,
            openalex=openalex,
            new_pmids=new_pmids,
            pmid_to_oa_refs=pmid_to_oa_refs,
            pmid_to_key=pmid_to_key,
            gene_symbol=symbol,
        )

    logger.info(
        f"{symbol}: search_added={search_stats['added']}, "
        f"cit_candidates={len(candidates)}, cit_added={cit_added}, "
        f"recent_added={recent_added}"
    )

    return {
        "symbol": symbol,
        "found": len(all_records),
        "new": len(new_records),
        **search_stats,
        "cit_candidates": len(candidates),
        "cit_added": cit_added,
        "recent_added": recent_added,
    }


def process_topic_subtopic(
    sub_topic: dict,
    category_cfg: dict,
    topic_cfg: dict,
    category_parent_key: str,
    zot: ZoteroGroupClient,
    existing_pmids: set[str],
    pmid_to_key: dict[str, str],
    openalex: OpenAlexClient,
    rejection_log: RejectionLog | None = None,
    citation_cache: dict | None = None,
) -> dict:
    """Process a single sub-topic within a category. Returns stats dict."""

    sub_name = sub_topic["name"]
    collection_name = sub_topic.get("collection", sub_name)
    category_name = category_cfg["name"]

    # Merge citation expansion config: category overrides global
    global_cit = topic_cfg.get("citation_expansion", {})
    cat_cit = category_cfg.get("citation_expansion", {})
    cit_cfg = {**global_cit, **cat_cit}
    cit_enabled = cit_cfg.get("enabled", True)

    # Search settings
    global_search = topic_cfg.get("search", {})
    max_results = sub_topic.get("max_results", global_search.get("max_results", 50))
    recent_max_results = global_search.get("recent_max_results", 5)

    # Type filter: sub_topic > category > global
    type_filter = (
        sub_topic.get("type_filter")
        or category_cfg.get("type_filter")
        or global_search.get("type_filter")
    )

    # OpenAlex scoping
    scoping = topic_cfg.get("openalex_scoping", {})
    subfield_id = scoping.get("ophthalmology_subfield")
    topic_ids = category_cfg.get("topic_ids")
    if topic_ids:
        topic_ids = [str(t) for t in topic_ids]

    # Exclusions
    text_excl = topic_cfg.get("default_exclusions_text", [])
    mesh_excl = topic_cfg.get("default_exclusions_mesh", [])

    # Clinical keywords (pathologies only)
    clinical_keywords = category_cfg.get("clinical_keywords")

    # Keywords and mention terms
    keywords = _get_subtopic_keywords(sub_topic)
    mention_terms = _get_mention_terms(sub_topic)

    # Track for relation linking
    pmid_to_oa_refs: dict[str, list[str]] = {}
    new_pmids: set[str] = set()

    logger.info(f"{'=' * 60}")
    logger.info(f"Processing topic: {category_name} / {sub_name}")
    logger.info(f"{'=' * 60}")

    if rejection_log:
        rejection_log.set_context(subcollection=sub_name, category=category_name)

    # 1. Get/create nested Zotero collection
    collection_key = zot.get_or_create_collection(
        collection_name, parent_key=category_parent_key
    )

    # 2. Check existing papers in this sub-topic's collection
    collection_pmids = zot.get_collection_pmids(collection_key)
    search_stats = {"added": 0, "failed": 0}
    new_records: list[dict] = []

    if collection_pmids:
        # Collection has papers -- use as citation seeds, skip search
        logger.info(
            f"{sub_name}: {len(collection_pmids)} existing papers, "
            f"using as citation seeds"
        )
        seed_works = openalex.fetch_works_by_pmids(collection_pmids)
        all_records = seed_works
        library_size = len(collection_pmids)
    else:
        # Empty collection -- run OpenAlex search
        logger.info(f"{sub_name}: collection empty, running OpenAlex search")
        seed_works = openalex.search_topic(
            keywords=keywords,
            exclude_terms=text_excl,
            exclude_mesh=mesh_excl,
            max_results=max_results,
            type_filter=type_filter,
            subfield_id=subfield_id,
            topic_ids=topic_ids,
            clinical_keywords=clinical_keywords,
            rejection_log=rejection_log,
        )

        if not seed_works:
            logger.info(f"No results for {sub_name}")
            return {
                "name": f"{category_name}/{sub_name}",
                "found": 0, "new": 0, "added": 0, "failed": 0,
                "cit_candidates": 0, "cit_added": 0, "recent_added": 0,
            }

        # Preserve referenced_works
        for w in seed_works:
            pmid = OpenAlexClient.extract_pmid(w)
            if pmid:
                pmid_to_oa_refs[pmid] = w.get("referenced_works", [])

        all_records = [OpenAlexClient.work_to_record(w) for w in seed_works]
        new_records = [
            r for r in all_records
            if r.get("pmid") and r["pmid"] not in existing_pmids
        ]

        logger.info(
            f"{sub_name}: {len(all_records)} from OpenAlex, "
            f"{len(new_records)} new"
        )

        if new_records:
            extra_tags = [category_name, sub_name]
            search_stats = zot.add_papers(
                new_records,
                collection_key=collection_key,
                gene_symbol=None,
                extra_tags=extra_tags,
                source_tag="source:search",
            )
            pmid_to_key.update(search_stats.get("pmid_to_key", {}))
            for r in new_records:
                existing_pmids.add(r["pmid"])
                new_pmids.add(r["pmid"])

        library_size = len(all_records)

    # 3. Citation expansion (if enabled)
    candidates: list[dict] = []
    cit_added = 0
    if cit_enabled and seed_works:
        candidates = openalex.expand_citations(
            seed_works=seed_works,
            existing_pmids=existing_pmids,
            library_size=library_size,
            max_seeds=cit_cfg.get("max_seed_papers", 30),
            min_co_citations=cit_cfg.get("min_co_citations", 2),
            max_min_co=cit_cfg.get("max_min_co", 4),
            mention_terms=mention_terms,
            max_hops=cit_cfg.get("max_hops", 1),
            hop2_top_n=cit_cfg.get("hop2_top_n", 5),
            exclude_mesh=mesh_excl or None,
            rejection_log=rejection_log,
            citation_cache=citation_cache,
        )

        if candidates:
            for c in candidates:
                w = c.get("work", {})
                pmid = OpenAlexClient.extract_pmid(w)
                if pmid:
                    pmid_to_oa_refs.setdefault(pmid, w.get("referenced_works", []))

            candidate_records = [
                OpenAlexClient.work_to_record(c["work"]) for c in candidates
            ]

            # Text filter
            candidate_records = filter_records_by_text(candidate_records, text_excl, rejection_log)

            candidate_records = [
                r for r in candidate_records
                if r.get("pmid") and r["pmid"] not in existing_pmids
            ]

            if candidate_records:
                extra_tags = [category_name, sub_name]
                cit_stats = zot.add_papers(
                    candidate_records,
                    collection_key=collection_key,
                    gene_symbol=None,
                    extra_tags=extra_tags,
                    source_tag="source:citation",
                )
                cit_added = cit_stats["added"]
                pmid_to_key.update(cit_stats.get("pmid_to_key", {}))
                for r in candidate_records:
                    existing_pmids.add(r["pmid"])
                    new_pmids.add(r["pmid"])

    # 4. Recent papers pass
    recent_added = 0
    recent_works = openalex.search_topic_recent(
        keywords=keywords,
        max_results=recent_max_results,
        type_filter=type_filter,
        subfield_id=subfield_id,
        topic_ids=topic_ids,
    )
    if recent_works:
        if mesh_excl:
            recent_works = openalex.filter_by_mesh(
                recent_works, mesh_excl, rejection_log=rejection_log
            )
        for w in recent_works:
            pmid = OpenAlexClient.extract_pmid(w)
            if pmid:
                pmid_to_oa_refs.setdefault(pmid, w.get("referenced_works", []))

        recent_records = [OpenAlexClient.work_to_record(w) for w in recent_works]

        recent_records = filter_records_by_text(recent_records, text_excl, rejection_log)

        recent_records = [
            r for r in recent_records
            if r.get("pmid") and r["pmid"] not in existing_pmids
        ]

        logger.info(
            f"{sub_name}: recent-papers pass: {len(recent_works)} found, "
            f"{len(recent_records)} new after dedup"
        )

        if recent_records:
            extra_tags = [category_name, sub_name]
            rec_stats = zot.add_papers(
                recent_records,
                collection_key=collection_key,
                gene_symbol=None,
                extra_tags=extra_tags,
                source_tag="source:recent",
            )
            recent_added = rec_stats["added"]
            pmid_to_key.update(rec_stats.get("pmid_to_key", {}))
            for r in recent_records:
                existing_pmids.add(r["pmid"])
                new_pmids.add(r["pmid"])

    # 5. Link relations
    if new_pmids:
        _link_relations(
            zot=zot,
            openalex=openalex,
            new_pmids=new_pmids,
            pmid_to_oa_refs=pmid_to_oa_refs,
            pmid_to_key=pmid_to_key,
            gene_symbol=f"{category_name}/{sub_name}",
        )

    logger.info(
        f"{category_name}/{sub_name}: search_added={search_stats['added']}, "
        f"cit_candidates={len(candidates)}, cit_added={cit_added}, "
        f"recent_added={recent_added}"
    )

    return {
        "name": f"{category_name}/{sub_name}",
        "found": len(all_records),
        "new": len(new_records),
        "added": search_stats["added"],
        "failed": search_stats["failed"],
        "cit_candidates": len(candidates),
        "cit_added": cit_added,
        "recent_added": recent_added,
    }


def main():
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(f"logs/run_{datetime.date.today()}.log"),
        ],
    )

    import argparse

    parser = argparse.ArgumentParser(description="Gene & Topic Literature Bot")
    parser.add_argument(
        "--genes", nargs="*", default=None,
        help="Run gene pipeline. Optionally specify gene symbols.",
    )
    parser.add_argument(
        "--topics", nargs="?", const="ALL", default=None,
        help="Run topic pipeline. Optionally specify a category name.",
    )
    args = parser.parse_args()

    # Determine mode
    # No flags = both pipelines (default for cron)
    # --genes = gene pipeline only (optionally with specific symbols)
    # --topics = topic pipeline only (optionally with category filter)
    # --genes --topics = both pipelines
    run_genes = False
    run_topics = False
    gene_symbols: list[str] | None = None
    topic_category_filter: str | None = None

    env_mode = os.environ.get("INPUT_MODE", "").lower()
    env_categories = os.environ.get("INPUT_CATEGORIES", "").strip()
    env_genes = os.environ.get("INPUT_GENES", "").split()

    if args.genes is None and args.topics is None and (not env_mode or env_mode == "both"):
        # No flags at all -> run both pipelines
        run_genes = True
        run_topics = True
    else:
        if args.genes is not None or env_mode in ("genes", "all"):
            run_genes = True
            if args.genes:
                gene_symbols = args.genes
            elif env_genes and env_genes != [""]:
                gene_symbols = env_genes

        if args.topics is not None or env_mode in ("topics", "all"):
            run_topics = True
            if args.topics and args.topics != "ALL":
                topic_category_filter = args.topics
            elif env_categories:
                topic_category_filter = env_categories

    # Credentials from env
    zotero_api_key = os.environ.get("ZOTERO_API_KEY")
    zotero_group_id = os.environ.get("ZOTERO_GROUP_ID")

    if not zotero_api_key or not zotero_group_id:
        logger.error(
            "Missing required env vars. Set: ZOTERO_API_KEY, ZOTERO_GROUP_ID"
        )
        sys.exit(1)

    # OpenAlex client
    openalex_key = os.environ.get("OPENALEX_API_KEY") or None
    openalex = OpenAlexClient(api_key=openalex_key)
    if openalex_key:
        logger.info("OpenAlex client initialized (with API key)")
    else:
        logger.info("OpenAlex client initialized (no API key -- lower rate limit)")

    # Init Zotero client and fetch existing items once (pmid -> zotero_key)
    # Shared between gene and topic pipelines for cross-deduplication.
    zot = ZoteroGroupClient(
        group_id=zotero_group_id,
        api_key=zotero_api_key,
        delay=1.0,
    )
    pmid_to_key: dict[str, str] = zot.get_existing_items()
    existing_pmids: set[str] = set(pmid_to_key.keys())

    trashed_pmids = zot.get_trashed_pmids()
    if trashed_pmids:
        logger.info(f"Blocking {len(trashed_pmids)} trashed PMIDs from re-upload")
        existing_pmids.update(trashed_pmids)

    rejection_log = RejectionLog()

    # Load citation cache for incremental expansion
    citation_cache = load_citation_cache()
    if citation_cache is None:
        citation_cache = {"version": 1, "seeds": {}, "last_run_date": ""}

    # -----------------------------------------------------------------
    # Gene pipeline
    # -----------------------------------------------------------------
    if run_genes:
        config = load_genes_config()
        default_excl_text = config.get("default_exclusions_text", [])
        default_excl_mesh = config.get("default_exclusions_mesh", [])
        all_genes = config.get("genes", [])
        collections_cfg = config.get("collections", {})
        genes_parent_name = collections_cfg.get("genes_parent")
        citation_cfg = config.get("citation_expansion", {})
        search_cfg = config.get("search", {})
        search_max_results = search_cfg.get("max_results", 25)
        recent_max_results = search_cfg.get("recent_max_results", 10)

        genes_to_run = []
        if gene_symbols:
            known = {g["symbol"]: g for g in all_genes}
            for s in gene_symbols:
                if s in known:
                    genes_to_run.append(known[s])
                else:
                    genes_to_run.append({"symbol": s})
        else:
            genes_to_run = all_genes

        # Determine which genes get full forward expansion this week
        manual_run = gene_symbols is not None
        if manual_run:
            full_expansion_genes = {g["symbol"] for g in genes_to_run}
        else:
            full_expansion_genes = select_rotation_genes(all_genes, citation_cache)
        logger.info(
            f"Full forward expansion for {len(full_expansion_genes)} genes "
            f"(rotation): {sorted(full_expansion_genes)}"
        )

        if not genes_to_run:
            logger.warning("No genes to process. Add genes to genes.yml or pass as arguments.")
        else:
            logger.info(f"Genes to process: {[g['symbol'] for g in genes_to_run]}")

            # Ensure parent collection exists once
            genes_parent_key: str | None = None
            if genes_parent_name:
                genes_parent_key = zot.get_or_create_collection(genes_parent_name)
                logger.info(f"Using parent collection '{genes_parent_name}' (key={genes_parent_key})")

            # Process each gene
            gene_summary = []
            for gene_cfg in genes_to_run:
                stats = process_gene(
                    gene_cfg=gene_cfg,
                    default_excl_text=default_excl_text,
                    default_excl_mesh=default_excl_mesh,
                    genes_parent_key=genes_parent_key,
                    zot=zot,
                    existing_pmids=existing_pmids,
                    pmid_to_key=pmid_to_key,
                    openalex=openalex,
                    citation_cfg=citation_cfg,
                    search_max_results=search_max_results,
                    recent_max_results=recent_max_results,
                    rejection_log=rejection_log,
                    citation_cache=citation_cache,
                    force_full_expansion=gene_cfg["symbol"] in full_expansion_genes,
                )
                gene_summary.append(stats)

            # Print gene summary
            logger.info("")
            logger.info("=" * 60)
            logger.info("GENE SUMMARY")
            logger.info("=" * 60)
            for s in gene_summary:
                logger.info(
                    f"  {s['symbol']:12s}  found={s['found']:4d}  "
                    f"new={s['new']:4d}  added={s['added']:4d}  failed={s['failed']:4d}  "
                    f"cit_cand={s['cit_candidates']:4d}  cit_add={s['cit_added']:4d}  "
                    f"recent_add={s['recent_added']:4d}"
                )

    # -----------------------------------------------------------------
    # Topic pipeline
    # -----------------------------------------------------------------
    if run_topics:
        topic_cfg = load_topics_config()
        if not topic_cfg or not topic_cfg.get("categories"):
            logger.warning("No topics configured in topics.yml, skipping topic pipeline")
        else:
            categories = topic_cfg["categories"]

            # Resolve category alias (e.g. "anatomy" -> "1 - Anatomy")
            if topic_category_filter:
                alias_map = {
                    c["alias"].lower(): c["name"]
                    for c in categories
                    if c.get("alias")
                }
                topic_category_filter = alias_map.get(
                    topic_category_filter.lower(), topic_category_filter
                )

            # Filter to specific category if requested
            if topic_category_filter:
                categories = [
                    c for c in categories
                    if c["name"] == topic_category_filter
                ]
                if not categories:
                    logger.error(
                        f"Category '{topic_category_filter}' not found in topics.yml. "
                        f"Available: {[c['name'] for c in topic_cfg['categories']]}"
                    )

            topic_summary = []
            for cat_cfg in categories:
                cat_name = cat_cfg["name"]
                logger.info(f"\n{'#' * 60}")
                logger.info(f"CATEGORY: {cat_name}")
                logger.info(f"{'#' * 60}")

                # Ensure category-level collection exists
                cat_key = zot.get_or_create_collection(cat_name)

                for sub_topic in cat_cfg.get("sub_topics", []):
                    stats = process_topic_subtopic(
                        sub_topic=sub_topic,
                        category_cfg=cat_cfg,
                        topic_cfg=topic_cfg,
                        category_parent_key=cat_key,
                        zot=zot,
                        existing_pmids=existing_pmids,
                        pmid_to_key=pmid_to_key,
                        openalex=openalex,
                        rejection_log=rejection_log,
                        citation_cache=citation_cache,
                    )
                    topic_summary.append(stats)

            # Print topic summary
            if topic_summary:
                logger.info("")
                logger.info("=" * 60)
                logger.info("TOPIC SUMMARY")
                logger.info("=" * 60)
                for s in topic_summary:
                    logger.info(
                        f"  {s['name']:40s}  found={s['found']:4d}  "
                        f"new={s['new']:4d}  added={s['added']:4d}  "
                        f"cit_cand={s['cit_candidates']:4d}  cit_add={s['cit_added']:4d}  "
                        f"recent_add={s['recent_added']:4d}"
                    )


    # -----------------------------------------------------------------
    # Save citation cache for next run
    # -----------------------------------------------------------------
    save_citation_cache(citation_cache)

    if openalex.failed_request_count > 0:
        logger.warning(f"OpenAlex API: {openalex.failed_request_count} request(s) failed after retries")

    # -----------------------------------------------------------------
    # Write rejection log for near-miss dashboard (cumulative merge)
    # -----------------------------------------------------------------
    os.makedirs("data", exist_ok=True)
    previous_path = "data/previous_near_misses.json"
    if not os.path.isfile(previous_path):
        previous_path = None
    rejection_log.to_json("data/near_misses.json", previous_path=previous_path)


if __name__ == "__main__":
    main()
