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
from genebot.pubmed import build_query, search_pubmed
from genebot.filters import filter_by_text
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
    default_excl_mesh: list[str],
    default_excl_text: list[str],
    genes_parent_key: str | None,
    zot: ZoteroGroupClient,
    existing_pmids: set[str],
    ncbi_email: str,
    ncbi_api_key: str | None,
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

    # 2. PubMed search
    query = build_query(aliases, mesh_excl)
    records = search_pubmed(
        query=query,
        email=ncbi_email,
        api_key=ncbi_api_key,
        delay=0.11 if ncbi_api_key else 0.34,
    )

    if not records:
        logger.info(f"No results for {symbol}")
        return {"symbol": symbol, "found": 0, "new": 0, "added": 0, "failed": 0}

    # 3. Text filter
    records = filter_by_text(records, text_excl)

    # 4. Dedup against existing library
    new_records = [r for r in records if r.get("pmid") and r["pmid"] not in existing_pmids]
    logger.info(
        f"{symbol}: {len(records)} after filters, "
        f"{len(new_records)} new ({len(records) - len(new_records)} already in library)"
    )

    if not new_records:
        return {"symbol": symbol, "found": len(records), "new": 0, "added": 0, "failed": 0}

    # 5. Get/create nested collection: "6 - Genes" > "CRB1"
    if genes_parent_key:
        collection_key = zot.get_or_create_collection(
            collection_name, parent_key=genes_parent_key
        )
    else:
        collection_key = zot.get_or_create_collection(collection_name)

    # 6. Upload with tags: gene symbol + disease groups (pub type tags added inside add_papers)
    upload_stats = zot.add_papers(
        new_records,
        collection_key=collection_key,
        gene_symbol=symbol,
        extra_tags=gene_tags,
    )

    # Track newly added PMIDs so subsequent genes don't re-add
    for r in new_records:
        existing_pmids.add(r["pmid"])

    return {
        "symbol": symbol,
        "found": len(records),
        "new": len(new_records),
        **upload_stats,
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

    # Load gene config
    config = load_genes_config()
    default_excl_mesh = config.get("default_exclusions_mesh", [])
    default_excl_text = config.get("default_exclusions_text", [])
    all_genes = config.get("genes", [])
    collections_cfg = config.get("collections", {})
    genes_parent_name = collections_cfg.get("genes_parent")

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
            f"new={s['new']:4d}  added={s['added']:4d}  failed={s['failed']:4d}"
        )


if __name__ == "__main__":
    main()
