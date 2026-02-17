# Gene Literature Bot -- CLAUDE.md

## Project overview

Automated OpenAlex-to-Zotero pipeline. Searches OpenAlex for gene-specific literature, expands results via citation network traversal with adaptive selectivity, deduplicates against an existing Zotero group library, and uploads new papers. Runs via GitHub Actions (weekly cron or manual trigger) or locally.

## Architecture

```
run.py                  # entrypoint -- orchestrates the full pipeline
genes.yml               # gene list + exclusion config + citation expansion settings
build_genes_yml.py      # generates genes.yml from vault gene notes
genebot/
  hgnc.py               # fetch gene aliases from HGNC REST API
  openalex.py           # OpenAlex API client (search + citation expansion)
  zotero_client.py      # Zotero group library client (pyzotero)
.github/workflows/
  genebot.yml           # GitHub Actions workflow (cron + manual dispatch)
```

## Pipeline flow

1. For each gene, fetch HGNC aliases (symbol + previous/alias names)
2. Search OpenAlex: `title_and_abstract.search` with gene symbol + aliases, filtered to `type:article,has_pmid:true`
3. Apply text exclusion filters, dedup against Zotero library
4. Upload new search-found papers with `source:search` tag
5. Citation expansion: resolve search results via OpenAlex, expand one hop (references + forward citations), score by co-citation count with recency bonus, apply adaptive threshold based on library size, fetch full metadata, filter, dedup, upload with `source:citation` tag

### Adaptive selectivity

The more papers already exist in the library for a gene, the higher the bar for new citation-discovered candidates:
- `min_co_citations` scales with `log2(library_size)` -- a gene with 4 papers needs 2 co-citations, a gene with 64 papers needs 6
- Recency bonus: papers from the last 3 years get up to +3 on their effective score, so recent papers can pass with fewer co-citations
- This prevents well-covered genes from accumulating tangential papers while still allowing fresh discoveries

## Running locally

```bash
pip install -r requirements.txt

# Required env vars
export ZOTERO_API_KEY="xxx"
export ZOTERO_GROUP_ID="123456"

# Optional (higher OpenAlex rate limit)
export OPENALEX_API_KEY="xxx"

python run.py              # all genes from genes.yml
python run.py CRB1 RHO     # specific genes only
```

Logs are written to `logs/run_YYYY-MM-DD.log`.

## Key behaviors

- **Idempotent**: only adds papers not already in the library (dedup by PMID)
- **OpenAlex-only**: no PubMed/Entrez dependency; searches via OpenAlex full-text index
- **Citation network expansion**: one-hop forward/backward citations via OpenAlex. Scored by co-citation count with recency bonus. Adaptive threshold scales with library size.
- **Provenance tagging**: papers tagged `source:search` or `source:citation` to track discovery method
- **Text exclusion filters**: whole-word regex on title (e.g., "cancer", "tumor")
- **Batch uploads**: 50 items per Zotero API request
- **Gene selection priority**: CLI args > `INPUT_GENES` env var > all genes in `genes.yml`
- Unknown gene symbols passed via CLI/env get default exclusions (no custom config required)

## Configuration

### genes.yml

```yaml
collections:
  genes_parent: "6 - Genes"

citation_expansion:
  max_seed_papers: 100    # expand citations for top N most-cited seeds per gene
  min_co_citations: 1     # minimum co-citation count (before adaptive scaling)

default_exclusions_text:
  - cancer
  - tumor
  # ...

genes:
  - symbol: PAX6
    collection: PAX6
    tags:
      - aniridia
      - anterior-segment-dysgenesis
    exclude_text:           # optional per-gene override
      - "pancreatic"
```

## GitHub Actions secrets required

| Secret | Purpose |
|---|---|
| `ZOTERO_API_KEY` | Read/Write access to group |
| `ZOTERO_GROUP_ID` | Numeric group ID from Zotero URL |
| `OPENALEX_API_KEY` | Optional; OpenAlex API key for higher rate limit |

## Dependencies

- `pyzotero` -- Zotero API client
- `requests` -- HGNC REST API + OpenAlex API
- `pyyaml` -- config parsing

Python 3.12+.
