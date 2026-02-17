#!/usr/bin/env python3
"""
Gene Literature Bot
Exhaustive PubMed search -> Zotero group library.

Reads gene list from genes.yml, credentials from environment variables.

Local usage:
    export NCBI_EMAIL="you@example.com"
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
from genebot.pubmed import build_query, search_pubmed, fetch_by_pmids
from genebot.filters import filter_by_text
from genebot.zotero_client import ZoteroGroupClient
from genebot.disease_terms import get_disease_query_terms
from genebot.openalex import OpenAlexClient

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
    default_excl_mesh: list[str],
    default_excl_text: list[str],
    genes_parent_key: str | None,
    zot: ZoteroGroupClient,
    existing_pmids: set[str],
    ncbi_email: str,
    ncbi_api_key: str | None,
    openalex_client: OpenAlexClient | None = None,
    citation_cfg: dict | None = None,
) -> dict:
    """Process a single gene. Returns stats dict."""

    symbol = gene_cfg["symbol"]
    collection_name = gene_cfg.get("collection", symbol)
    mesh_excl = gene_cfg.get("exclude_mesh", default_excl_mesh)
    text_excl = gene_cfg.get("exclude_text", default_excl_text)
    gene_tags = gene_cfg.get("tags", [])

    logger.info(f"{'=' * 60}")
    logger.info(f"Processing: {symbol}")
    logger.info(f"{'=' * 60}")

    # 1. Gene aliases
    aliases = get_gene_aliases(symbol)

    # 2. Disease-scoped PubMed search
    disease_terms = get_disease_query_terms(gene_tags)
    query = build_query(aliases, mesh_excl, disease_terms=disease_terms)
    pubmed_delay = 0.11 if ncbi_api_key else 0.34
    records = search_pubmed(
        query=query,
        email=ncbi_email,
        api_key=ncbi_api_key,
        delay=pubmed_delay,
    )

    if not records:
        logger.info(f"No results for {symbol}")
        return {
            "symbol": symbol, "found": 0, "new": 0, "added": 0, "failed": 0,
            "citation_candidates": 0, "citation_new": 0, "citation_added": 0,
        }

    # 3. Text filter
    records = filter_by_text(records, text_excl)

    # 4. Dedup against existing library
    new_records = [r for r in records if r.get("pmid") and r["pmid"] not in existing_pmids]
    logger.info(
        f"{symbol}: {len(records)} after filters, "
        f"{len(new_records)} new ({len(records) - len(new_records)} already in library)"
    )

    # 5. Get/create nested collection: "6 - Genes" > "CRB1"
    if genes_parent_key:
        collection_key = zot.get_or_create_collection(
            collection_name, parent_key=genes_parent_key
        )
    else:
        collection_key = zot.get_or_create_collection(collection_name)

    # 6. Upload PubMed-found papers
    pubmed_stats = {"added": 0, "failed": 0}
    if new_records:
        pubmed_stats = zot.add_papers(
            new_records,
            collection_key=collection_key,
            gene_symbol=symbol,
            extra_tags=gene_tags,
            source_tag="source:pubmed",
        )
        for r in new_records:
            existing_pmids.add(r["pmid"])

    # 7. Citation network expansion
    citation_candidates = 0
    citation_new = 0
    citation_added = 0

    if openalex_client and records:
        cfg = citation_cfg or {}
        max_seeds = cfg.get("max_seed_papers", 100)
        min_co = cfg.get("min_co_citations", 1)

        # Use all PubMed results (new + existing) as seeds
        seed_pmids = [r["pmid"] for r in records if r.get("pmid")]
        candidates = openalex_client.expand_seeds(seed_pmids, existing_pmids, max_seeds=max_seeds)

        # Filter by minimum co-citation count
        candidates = [c for c in candidates if c["co_citations"] >= min_co]
        citation_candidates = len(candidates)

        if candidates:
            # Fetch full Medline records for candidates
            candidate_pmids = [c["pmid"] for c in candidates]
            citation_records = fetch_by_pmids(
                candidate_pmids,
                email=ncbi_email,
                api_key=ncbi_api_key,
                delay=pubmed_delay,
            )

            # Apply same text filters
            citation_records = filter_by_text(citation_records, text_excl)

            # Final dedup (expand_seeds already filtered, but fetch_by_pmids may return extras)
            citation_new_records = [
                r for r in citation_records
                if r.get("pmid") and r["pmid"] not in existing_pmids
            ]
            citation_new = len(citation_new_records)

            if citation_new_records:
                cit_stats = zot.add_papers(
                    citation_new_records,
                    collection_key=collection_key,
                    gene_symbol=symbol,
                    extra_tags=gene_tags,
                    source_tag="source:citation",
                )
                citation_added = cit_stats["added"]
                for r in citation_new_records:
                    existing_pmids.add(r["pmid"])

            logger.info(
                f"{symbol} citations: {len(seed_pmids)} seeds -> "
                f"{citation_candidates} candidates -> {citation_new} new -> "
                f"{citation_added} added"
            )

    return {
        "symbol": symbol,
        "found": len(records),
        "new": len(new_records),
        **pubmed_stats,
        "citation_candidates": citation_candidates,
        "citation_new": citation_new,
        "citation_added": citation_added,
    }


def main():
    # Credentials from env
    ncbi_email = os.environ.get("NCBI_EMAIL")
    ncbi_api_key = os.environ.get("NCBI_API_KEY") or None
    zotero_api_key = os.environ.get("ZOTERO_API_KEY")
    zotero_group_id = os.environ.get("ZOTERO_GROUP_ID")

    if not ncbi_email or not zotero_api_key or not zotero_group_id:
        logger.error(
            "Missing required env vars. Set: NCBI_EMAIL, ZOTERO_API_KEY, ZOTERO_GROUP_ID"
        )
        sys.exit(1)

    # OpenAlex client (optional but recommended for citation expansion)
    openalex_key = os.environ.get("OPENALEX_API_KEY") or None
    openalex_client: OpenAlexClient | None = None
    if openalex_key:
        openalex_client = OpenAlexClient(api_key=openalex_key)
        logger.info("OpenAlex client initialized (with API key)")
    else:
        logger.info("OPENALEX_API_KEY not set -- citation expansion disabled")

    # Load gene config
    config = load_genes_config()
    default_excl_mesh = config.get("default_exclusions_mesh", [])
    default_excl_text = config.get("default_exclusions_text", [])
    all_genes = config.get("genes", [])
    collections_cfg = config.get("collections", {})
    genes_parent_name = collections_cfg.get("genes_parent")
    citation_cfg = config.get("citation_expansion", {})

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
        # Match against config; create default entry for unknown symbols
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

    # Ensure parent collection exists once (e.g. "6 - Genes"), reuse key for all genes
    genes_parent_key: str | None = None
    if genes_parent_name:
        genes_parent_key = zot.get_or_create_collection(genes_parent_name)
        logger.info(f"Using parent collection '{genes_parent_name}' (key={genes_parent_key})")

    # Process each gene
    summary = []
    for gene_cfg in genes_to_run:
        stats = process_gene(
            gene_cfg=gene_cfg,
            default_excl_mesh=default_excl_mesh,
            default_excl_text=default_excl_text,
            genes_parent_key=genes_parent_key,
            zot=zot,
            existing_pmids=existing_pmids,
            ncbi_email=ncbi_email,
            ncbi_api_key=ncbi_api_key,
            openalex_client=openalex_client,
            citation_cfg=citation_cfg,
        )
        summary.append(stats)

    # Print summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    for s in summary:
        cit_part = ""
        if s.get("citation_candidates", 0) > 0:
            cit_part = (
                f"  cit_cand={s['citation_candidates']:4d}  "
                f"cit_new={s['citation_new']:4d}  cit_add={s['citation_added']:4d}"
            )
        logger.info(
            f"  {s['symbol']:12s}  found={s['found']:4d}  "
            f"new={s['new']:4d}  added={s['added']:4d}  failed={s['failed']:4d}"
            f"{cit_part}"
        )


if __name__ == "__main__":
    main()
