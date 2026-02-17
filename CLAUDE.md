# Gene Literature Bot — CLAUDE.md

## Project overview

Automated PubMed-to-Zotero pipeline. Searches PubMed for gene-specific literature, deduplicates against an existing Zotero group library, and uploads new papers. Runs via GitHub Actions (weekly cron or manual trigger) or locally.

## Architecture

```
run.py                  # entrypoint — orchestrates the full pipeline
genes.yml               # gene list + exclusion config (edit this to add genes)
genebot/
  hgnc.py               # fetch gene aliases from HGNC REST API
  pubmed.py             # build PubMed query, fetch records via Biopython Entrez
  filters.py            # post-hoc free-text filter on title + abstract
  zotero_client.py      # Zotero group library client (pyzotero)
.github/workflows/
  genebot.yml           # GitHub Actions workflow (cron + manual dispatch)
```

## Running locally

```bash
pip install -r requirements.txt

# Required env vars
export NCBI_EMAIL="you@example.com"
export ZOTERO_API_KEY="xxx"
export ZOTERO_GROUP_ID="123456"

# Optional (increases NCBI rate limit from ~3 to ~10 req/s)
export NCBI_API_KEY="xxx"

python run.py              # all genes from genes.yml
python run.py CRB1 RHO     # specific genes only
```

Logs are written to `logs/run_YYYY-MM-DD.log`.

## Key behaviors

- **Idempotent**: only adds papers not already in the library (dedup by PMID)
- **Two-layer filtering**: MeSH exclusion (query-level) + free-text exclusion (post-hoc on title/abstract)
- **Batch uploads**: 50 items per Zotero API request
- **Gene selection priority**: CLI args > `INPUT_GENES` env var > all genes in `genes.yml`
- Unknown gene symbols passed via CLI/env get default exclusions (no custom config required)

## Adding genes

Edit `genes.yml`. Each gene inherits `default_exclusions_mesh` and `default_exclusions_text` unless overridden:

```yaml
genes:
  - symbol: PAX6
    collection: "PAX6 Literature"
    exclude_mesh:
      - "Neoplasms"
      - "Diabetes Mellitus"
    exclude_text:
      - "cancer"
      - "pancreatic"
```

## GitHub Actions secrets required

| Secret | Purpose |
|---|---|
| `NCBI_EMAIL` | Required by Entrez policy |
| `NCBI_API_KEY` | Optional; higher rate limit |
| `ZOTERO_API_KEY` | Read/Write access to group |
| `ZOTERO_GROUP_ID` | Numeric group ID from Zotero URL |

## Dependencies

- `biopython` — Entrez/PubMed queries
- `pyzotero` — Zotero API client
- `requests` — HGNC REST API
- `pyyaml` — config parsing

Python 3.12+.
