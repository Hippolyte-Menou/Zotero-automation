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
import sys
import logging
import datetime
import yaml

from genebot.hgnc import get_gene_aliases
from genebot.openalex import OpenAlexClient
from genebot.zotero_client import ZoteroGroupClient

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"logs/run_{datetime.date.today()}.log"),
    ],
)
logger = logging.getLogger("genebot")


def load_genes_config(path: str = "genes.yml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def process_gene(
    gene_cfg: dict,
    default_excl_text: list[str],
    genes_parent_key: str | None,
    zot: ZoteroGroupClient,
    existing_pmids: set[str],
    openalex: OpenAlexClient,
    citation_cfg: dict,
    search_max_results: int = 25,
    recent_max_results: int = 10,
) -> dict:
    """Process a single gene. Returns stats dict."""

    symbol = gene_cfg["symbol"]
    collection_name = gene_cfg.get("collection", symbol)
    text_excl = gene_cfg.get("exclude_text", default_excl_text)
    gene_tags = gene_cfg.get("tags", [])

    logger.info(f"{'=' * 60}")
    logger.info(f"Processing: {symbol}")
    logger.info(f"{'=' * 60}")

    # 1. Gene aliases from HGNC
    aliases = get_gene_aliases(symbol)
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
        )

        if not seed_works:
            logger.info(f"No results for {symbol}")
            return {
                "symbol": symbol, "found": 0, "new": 0,
                "added": 0, "failed": 0,
                "cit_candidates": 0, "cit_added": 0, "recent_added": 0
            }

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
            for r in new_records:
                existing_pmids.add(r["pmid"])

        library_size = len(all_records)

    # 4. Citation network expansion (multi-hop, gene-filtered, bib coupling)
    import re
    max_seeds = citation_cfg.get("max_seed_papers", 100)
    min_co = citation_cfg.get("min_co_citations", 1)
    max_min_co = citation_cfg.get("max_min_co", 6)
    max_hops = citation_cfg.get("max_hops", 2)
    hop2_top_n = citation_cfg.get("hop2_top_n", 10)

    candidates = openalex.expand_citations(
        seed_works=seed_works,
        existing_pmids=existing_pmids,
        library_size=library_size,
        max_seeds=max_seeds,
        min_co_citations=min_co,
        max_min_co=max_min_co,
        gene_terms=search_terms,
        max_hops=max_hops,
        hop2_top_n=hop2_top_n,
    )

    cit_added = 0
    if candidates:
        # Candidates already have full metadata attached (key 'work')
        candidate_records = [
            OpenAlexClient.work_to_record(c["work"]) for c in candidates
        ]

        # Text filter + dedup
        if text_excl:
            patterns = [
                re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE)
                for t in text_excl
            ]
            candidate_records = [
                r for r in candidate_records
                if not any(
                    p.search(f"{r.get('title', '')} {r.get('abstract', '')}")
                    for p in patterns
                )
            ]

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
            for r in candidate_records:
                existing_pmids.add(r["pmid"])

    # 5. Recent papers pass -- bypass citation threshold for current-year papers
    recent_works = openalex.search_gene_recent(
        search_terms,
        disease_keywords=gene_tags or None,
        max_results=recent_max_results,
    )
    recent_added = 0
    if recent_works:
        recent_records = [
            OpenAlexClient.work_to_record(w) for w in recent_works
        ]

        # Text filter
        if text_excl:
            patterns = [
                re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE)
                for t in text_excl
            ]
            recent_records = [
                r for r in recent_records
                if not any(
                    p.search(f"{r.get('title', '')} {r.get('abstract', '')}")
                    for p in patterns
                )
            ]

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
            for r in recent_records:
                existing_pmids.add(r["pmid"])

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


def main():
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

    # Load gene config
    config = load_genes_config()
    default_excl_text = config.get("default_exclusions_text", [])
    all_genes = config.get("genes", [])
    collections_cfg = config.get("collections", {})
    genes_parent_name = collections_cfg.get("genes_parent")
    citation_cfg = config.get("citation_expansion", {})
    search_cfg = config.get("search", {})
    search_max_results = search_cfg.get("max_results", 25)
    recent_max_results = search_cfg.get("recent_max_results", 10)

    # Determine which genes to run
    # Priority: CLI args > INPUT_GENES env var > all from genes.yml
    cli_genes = sys.argv[1:]
    env_genes = os.environ.get("INPUT_GENES", "").split()

    if cli_genes:
        target_symbols = set(cli_genes)
    elif env_genes and env_genes != [""]:
        target_symbols = set(env_genes)
    else:
        target_symbols = None  # run all

    genes_to_run = []
    if target_symbols:
        known = {g["symbol"]: g for g in all_genes}
        for s in target_symbols:
            if s in known:
                genes_to_run.append(known[s])
            else:
                genes_to_run.append({"symbol": s})
    else:
        genes_to_run = all_genes

    if not genes_to_run:
        logger.error("No genes to process. Add genes to genes.yml or pass as arguments.")
        sys.exit(1)

    logger.info(f"Genes to process: {[g['symbol'] for g in genes_to_run]}")

    # Init Zotero client and fetch existing PMIDs once
    zot = ZoteroGroupClient(
        group_id=zotero_group_id,
        api_key=zotero_api_key,
        delay=1.0,
    )
    existing_pmids = zot.get_existing_pmids()

    # Ensure parent collection exists once
    genes_parent_key: str | None = None
    if genes_parent_name:
        genes_parent_key = zot.get_or_create_collection(genes_parent_name)
        logger.info(f"Using parent collection '{genes_parent_name}' (key={genes_parent_key})")

    # Process each gene
    summary = []
    for gene_cfg in genes_to_run:
        stats = process_gene(
            gene_cfg=gene_cfg,
            default_excl_text=default_excl_text,
            genes_parent_key=genes_parent_key,
            zot=zot,
            existing_pmids=existing_pmids,
            openalex=openalex,
            citation_cfg=citation_cfg,
            search_max_results=search_max_results,
            recent_max_results=recent_max_results,
        )
        summary.append(stats)

    # Print summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    for s in summary:
        logger.info(
            f"  {s['symbol']:12s}  found={s['found']:4d}  "
            f"new={s['new']:4d}  added={s['added']:4d}  failed={s['failed']:4d}  "
            f"cit_cand={s['cit_candidates']:4d}  cit_add={s['cit_added']:4d}  "
            f"recent_add={s['recent_added']:4d}"
        )


if __name__ == "__main__":
    main()
