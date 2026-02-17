# Gene Literature Bot -- CLAUDE.md

## Project overview

Automated OpenAlex-to-Zotero pipeline. Searches OpenAlex for gene-specific literature using disease keyword filtering, expands results via multi-hop citation network traversal with bibliographic coupling and gene-name filtering, deduplicates against an existing Zotero group library, and uploads new papers. Runs via GitHub Actions (weekly cron or manual trigger) or locally.

## Architecture

```
run.py                  # entrypoint -- orchestrates the full pipeline
genes.yml               # gene list + exclusion config + search/citation settings
build_genes_yml.py      # generates genes.yml from vault gene notes
genebot/
  hgnc.py               # fetch gene aliases from HGNC REST API
  openalex.py           # OpenAlex API client (search + citation expansion)
  zotero_client.py      # Zotero group library client (pyzotero + httpx)
.github/workflows/
  genebot.yml           # GitHub Actions workflow (cron + manual dispatch)
```

## Pipeline flow

1. For each gene, fetch HGNC aliases (symbol + previous/alias names)
2. Search OpenAlex: `(gene OR aliases) AND (disease keywords)` using boolean syntax in `title_and_abstract.search`, filtered to `type:article,has_pmid:true`, capped at `search.max_results` (default 25)
3. Apply text exclusion filters, dedup against Zotero library
4. Upload new search-found papers with `source:search` tag
5. Multi-hop citation expansion (default 2 hops):
   - Hop 1: expand search results via backward references + forward citations
   - Score candidates by co-citation count + bibliographic coupling (shared references with seed set, capped at +3) + recency bonus (up to +3 for papers < 3 years old)
   - Apply adaptive threshold based on library size
   - Fetch full metadata, filter by gene-name presence in title/abstract
   - Hop 2: top N candidates from hop 1 become new seeds, repeat expansion
   - Merge, deduplicate, text-filter, upload with `source:citation` tag

### Disease keyword search

The `tags` field in genes.yml provides disease keywords. Tag slugs are converted to search terms (`retinitis-pigmentosa` -> `"retinitis pigmentosa"`). The OpenAlex query becomes `(ABCA4 OR ARMD2 OR STGD) AND ("stargardt disease" OR "retinitis pigmentosa" OR ...)`, keeping results focused on the gene's ophthalmic context.

### Adaptive selectivity

The more papers already exist in the library for a gene, the higher the bar for new citation-discovered candidates:
- `min_co_citations` scales with `log2(library_size)` -- a gene with 4 papers needs 2 co-citations, a gene with 64 papers needs 6
- `max_min_co` (default 6) caps the adaptive threshold so mature libraries don't freeze out new discoveries
- Recency bonus: papers from the last 3 years get up to +3 on their effective score
- Bibliographic coupling bonus: candidates sharing references with the seed set get up to +3
- Gene-name filter: candidates must mention the gene symbol or aliases in their title or abstract

### Multi-hop expansion

Top candidates from each hop become seeds for the next hop, discovering papers that don't directly cite the original search results but are in the same research neighborhood. Controlled by `max_hops` (default 2) and `hop2_top_n` (default 10).

### Recent papers pass

After citation expansion, a separate OpenAlex search filtered to the current year retrieves up to `recent_max_results` (default 10) new papers per gene. These bypass the adaptive citation threshold entirely, ensuring current-year publications are not missed because they haven't yet accumulated enough citations or bibliographic coupling. Uploaded with `source:recent` tag.

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
- **Disease-focused search**: boolean AND with disease keywords from tags, capped at configurable max_results
- **Multi-hop citation expansion**: 2-hop forward/backward citations via OpenAlex with gene-name filtering and bibliographic coupling
- **Provenance tagging**: papers tagged `source:search`, `source:citation`, or `source:recent` to track discovery method
- **Recent papers pass**: current-year OpenAlex search bypasses citation threshold; catches new publications before they accumulate citations
- **Text exclusion filters**: whole-word regex on title and abstract (e.g., "cancer", "tumor")
- **Batch uploads**: 50 items per Zotero API request with retry on timeout
- **Zotero resilience**: 60s read timeout, 3-attempt retry with backoff on timeouts
- **Gene selection priority**: CLI args > `INPUT_GENES` env var > all genes in `genes.yml`
- Unknown gene symbols passed via CLI/env get default exclusions (no custom config required)

## Configuration

### genes.yml

```yaml
collections:
  genes_parent: "6 - Genes"

search:
  max_results: 25          # cap OpenAlex search results per gene
  recent_max_results: 10   # cap recent-papers pass results per gene

citation_expansion:
  max_seed_papers: 100     # expand citations for top N most-cited seeds per gene
  min_co_citations: 1      # minimum co-citation count (before adaptive scaling)
  max_min_co: 6            # cap on adaptive threshold (prevents mature genes from freezing)
  max_hops: 2              # number of citation expansion hops
  hop2_top_n: 10           # how many hop-1 candidates become hop-2 seeds

default_exclusions_text:
  - cancer
  - tumor
  # ...

genes:
  - symbol: PAX6
    collection: PAX6
    tags:                    # used as disease keywords in search AND clause
      - aniridia
      - anterior-segment-dysgenesis
    exclude_text:            # optional per-gene override
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
- `httpx` -- HTTP client with configurable timeouts (used by pyzotero)
- `requests` -- HGNC REST API + OpenAlex API
- `pyyaml` -- config parsing

Python 3.12+.
