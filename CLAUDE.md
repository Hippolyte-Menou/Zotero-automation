# Gene Literature Bot -- CLAUDE.md

## Project overview

Automated PubMed-to-Zotero pipeline. Searches PubMed for gene-specific literature with disease-scoped queries, expands results via OpenAlex citation networks, deduplicates against an existing Zotero group library, and uploads new papers. Runs via GitHub Actions (weekly cron or manual trigger) or locally.

## Architecture

```
run.py                  # entrypoint -- orchestrates the full pipeline
genes.yml               # gene list + exclusion config + citation expansion settings
genebot/
  hgnc.py               # fetch gene aliases from HGNC REST API
  pubmed.py             # build PubMed query, fetch records via Biopython Entrez
  filters.py            # post-hoc free-text filter on title + abstract
  zotero_client.py      # Zotero group library client (pyzotero)
  disease_terms.py      # tag-slug to PubMed MeSH/tiab term mapping
  openalex.py           # OpenAlex API client for citation network expansion
.github/workflows/
  genebot.yml           # GitHub Actions workflow (cron + manual dispatch)
```

## Pipeline flow

1. For each gene, fetch HGNC aliases
2. Build disease-scoped PubMed query: `(gene OR aliases) AND (disease MeSH terms)` -- disease terms derived from the gene's `tags` in `genes.yml` via `disease_terms.py`. Genes with no tags fall back to generic eye/retina/optic terms.
3. Fetch PubMed results, apply text exclusion filters, dedup against Zotero library
4. Upload new PubMed-found papers with `source:pubmed` tag
5. Citation expansion (requires `OPENALEX_API_KEY`): resolve seed PMIDs via OpenAlex, expand one hop (references + citations), score by co-citation count, fetch full records from PubMed, filter, dedup, upload with `source:citation` tag

## Running locally

```bash
pip install -r requirements.txt

# Required env vars
export NCBI_EMAIL="you@example.com"
export ZOTERO_API_KEY="xxx"
export ZOTERO_GROUP_ID="123456"

# Optional (increases NCBI rate limit from ~3 to ~10 req/s)
export NCBI_API_KEY="xxx"

# Optional (enables citation network expansion via OpenAlex)
export OPENALEX_API_KEY="xxx"

python run.py              # all genes from genes.yml
python run.py CRB1 RHO     # specific genes only
```

Logs are written to `logs/run_YYYY-MM-DD.log`.

## Key behaviors

- **Idempotent**: only adds papers not already in the library (dedup by PMID)
- **Disease-scoped queries**: PubMed queries AND'd with MeSH disease terms from gene tags. Prevents off-topic results (e.g., PAX6 in pancreatic research).
- **Citation network expansion**: one-hop forward/backward citations via OpenAlex. Scored by co-citation count (how many seed papers link to each candidate). Configurable via `citation_expansion` in `genes.yml`.
- **Provenance tagging**: papers tagged `source:pubmed` or `source:citation` to track discovery method
- **Two-layer filtering**: MeSH exclusion (query-level) + free-text exclusion (post-hoc on title/abstract)
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
  min_co_citations: 1     # minimum co-citation count to include a candidate

default_exclusions_mesh:
  - Neoplasms
  # ...

default_exclusions_text:
  - cancer
  # ...

genes:
  - symbol: PAX6
    collection: PAX6
    tags:
      - aniridia
      - anterior-segment-dysgenesis
    exclude_mesh:           # optional per-gene override
      - "Diabetes Mellitus"
    exclude_text:           # optional per-gene override
      - "pancreatic"
```

### Disease term mapping

`genebot/disease_terms.py` maps tag slugs (from `genes.yml` tags) to PubMed search terms. Each slug maps to one or more `"Term"[MeSH]` or `"term"[tiab]` entries. Genes with no mapped tags fall back to generic eye disease terms.

## GitHub Actions secrets required

| Secret | Purpose |
|---|---|
| `NCBI_EMAIL` | Required by Entrez policy |
| `NCBI_API_KEY` | Optional; higher rate limit |
| `ZOTERO_API_KEY` | Read/Write access to group |
| `ZOTERO_GROUP_ID` | Numeric group ID from Zotero URL |
| `OPENALEX_API_KEY` | Optional; OpenAlex API key for citation expansion |

## Dependencies

- `biopython` -- Entrez/PubMed queries
- `pyzotero` -- Zotero API client
- `requests` -- HGNC REST API + OpenAlex API
- `pyyaml` -- config parsing

Python 3.12+.
