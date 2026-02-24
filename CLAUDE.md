# Gene & Topic Literature Bot -- CLAUDE.md

## Project overview

Automated OpenAlex-to-Zotero pipeline with two complementary discovery modes:

1. **Gene pipeline**: searches OpenAlex for gene-specific literature using disease keyword filtering, expands results via multi-hop citation network traversal with bibliographic coupling and gene-name filtering
2. **Topic pipeline**: searches OpenAlex for ophthalmology topic literature (anatomy, embryology, physiology, examinations, pathologies) using keyword and subfield filters

Both pipelines deduplicate against an existing Zotero group library and share PMID state to avoid cross-pipeline duplicates. Runs via GitHub Actions (weekly cron or manual trigger) or locally.

## Architecture

```
run.py                  # entrypoint -- orchestrates both pipelines
genes.yml               # gene list + exclusion config + search/citation settings
topics.yml              # topic categories + sub-topics + keyword config
build_genes_yml.py      # generates genes.yml from vault gene notes
genebot/
  hgnc.py               # fetch gene aliases from HGNC REST API
  openalex.py           # OpenAlex API client (search + citation expansion + topic search)
  zotero_client.py      # Zotero group library client (pyzotero + httpx)
.github/workflows/
  genebot.yml           # GitHub Actions workflow (cron + manual dispatch)
```

## Gene pipeline flow

1. For each gene, fetch HGNC aliases (symbol + previous/alias names)
2. Search OpenAlex: `(gene OR aliases) AND (disease keywords)` using boolean syntax in `title_and_abstract.search`, filtered to `type:article,has_pmid:true`, capped at `search.max_results` (default 25)
3. Apply text exclusion filters, dedup against Zotero library
4. Upload new search-found papers with `source:search` tag
5. Multi-hop citation expansion (default 2 hops):
   - Hop 1: expand search results via backward references + forward citations
   - Score candidates by co-citation count + bibliographic coupling (shared references with seed set, capped at +3) + recency bonus (up to +3 for papers < 3 years old)
   - Apply adaptive threshold based on library size
   - Fetch full metadata, filter by mention-term presence in title/abstract
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
- Mention filter: candidates must mention the gene symbol or aliases in their title or abstract

### Multi-hop expansion

Top candidates from each hop become seeds for the next hop, discovering papers that don't directly cite the original search results but are in the same research neighborhood. Controlled by `max_hops` (default 2) and `hop2_top_n` (default 10).

### Recent papers pass

After citation expansion, a separate OpenAlex search filtered to the current year retrieves up to `recent_max_results` (default 10) new papers per gene. These bypass the adaptive citation threshold entirely, ensuring current-year publications are not missed because they haven't yet accumulated enough citations or bibliographic coupling. Uploaded with `source:recent` tag.

## Topic pipeline flow

1. Load topic categories and sub-topics from `topics.yml`
2. For each sub-topic, search OpenAlex using one of two modes:
   - **Keyword OR mode** (categories 1-4): all keywords OR'd into one query, filtered by ophthalmology subfield and optional topic IDs
   - **Disease+clinical AND mode** (category 5 - Pathologies): for each disease keyword, AND with clinical keywords, merge results with per-disease cap
3. Apply text exclusion and MeSH exclusion filters, dedup against Zotero library
4. Upload new papers with `source:search` tag, tagged with category and sub-topic names
5. Citation expansion (if enabled): expand via backward/forward citations with mention-term filtering
6. Recent papers pass: current-year search for new publications
7. Link related items via Zotero dc:relation

### OpenAlex filters for topics

Topic searches use OpenAlex structured filters:
- `primary_topic.subfield.id:2731` -- ophthalmology subfield (scopes all queries)
- `topics.id:T10250|T10170|...` -- optional topic ID whitelist (OR'd)
- `type:review|article` -- configurable per category (reviews preferred for categories 1-4)
- `title_and_abstract.search:{keywords}` -- keyword matching
- `has_pmid:true,is_retracted:false,language:en|fr` -- standard quality filters

### Config inheritance

Sub-topic settings inherit from category defaults, which inherit from global defaults:
- `search.max_results`, `search.recent_max_results`
- `citation_expansion` settings
- `exclude_text`, `exclude_mesh`
- `openalex_scoping` (subfield_id, topic_ids, type_filter)

## Running locally

```bash
pip install -r requirements.txt

# Required env vars
export ZOTERO_API_KEY="xxx"
export ZOTERO_GROUP_ID="123456"

# Optional (higher OpenAlex rate limit)
export OPENALEX_API_KEY="xxx"

python run.py                          # both pipelines (default)
python run.py --genes                  # all genes from genes.yml
python run.py --genes CRB1 RHO        # specific genes only
python run.py --topics                 # all topic categories
python run.py --topics anatomy         # specific category (alias)
python run.py --topics "1 - Anatomy"   # specific category (full name)
```

### Category aliases

Short aliases resolve to full category names from topics.yml:

| Alias | Resolves to |
|-------|-------------|
| `anatomy` | `1 - Anatomy` |
| `embryology` | `2 - Embryology` |
| `physiology` | `3 - Physiology` |
| `examinations` | `4 - Examinations` |
| `pathologies` | `5 - Pathologies` |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `INPUT_MODE` | Pipeline mode: `genes`, `topics`, or empty (both) |
| `INPUT_GENES` | Space-separated gene symbols (overrides genes.yml list) |
| `INPUT_CATEGORIES` | Category name filter for topic pipeline |

Logs are written to `logs/run_YYYY-MM-DD.log`.

## Key behaviors

- **Dual pipeline**: gene and topic discovery run in the same process, sharing dedup state
- **Idempotent**: only adds papers not already in the library (dedup by PMID)
- **Cross-pipeline dedup**: shared `existing_pmids` set prevents the same paper appearing in both gene and topic collections
- **OpenAlex-only**: no PubMed/Entrez dependency; searches via OpenAlex full-text index
- **Disease-focused search** (genes): boolean AND with disease keywords from tags
- **Keyword/subfield search** (topics): OR'd keywords scoped to ophthalmology subfield
- **Multi-hop citation expansion**: 2-hop forward/backward citations via OpenAlex with mention-term filtering and bibliographic coupling
- **Provenance tagging**: papers tagged `source:search`, `source:citation`, or `source:recent` to track discovery method
- **Recent papers pass**: current-year OpenAlex search bypasses citation threshold
- **Text exclusion filters**: whole-word regex on title and abstract (e.g., "cancer", "tumor")
- **MeSH exclusion filters**: exclude papers with specific MeSH descriptors (e.g., "Neoplasms")
- **Batch uploads**: 50 items per Zotero API request with retry on timeout
- **Zotero resilience**: 60s read timeout, 3-attempt retry with backoff on timeouts

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

default_exclusions_mesh:     # MeSH descriptor names excluded from all results
  - Neoplasms
  - Diabetes Mellitus
  # ...

genes:
  - symbol: PAX6
    collection: PAX6
    tags:                    # used as disease keywords in search AND clause
      - aniridia
      - anterior-segment-dysgenesis
    exclude_text:            # optional per-gene text exclusion override
      - "pancreatic"
    exclude_mesh:            # optional per-gene MeSH exclusion override
      - "Pancreatic Neoplasms"
```

### topics.yml

```yaml
collections:
  topics_parent: null        # top-level categories (no shared parent)

search:
  max_results: 30            # cap per sub-topic
  recent_max_results: 10

citation_expansion:
  enabled: true
  max_seed_papers: 50
  min_co_citations: 2
  max_min_co: 5
  max_hops: 1
  hop2_top_n: 5

openalex_scoping:
  subfield_id: 2731          # ophthalmology
  type_filter: "review|article"

default_exclusions_text:
  - cancer
  - tumor
  # ...

default_exclusions_mesh:
  - Neoplasms
  - Diabetes Mellitus
  # ...

categories:
  - name: "1 - Anatomy"
    openalex_scoping:
      topic_ids: ["T10250", "T10170"]   # optional topic whitelist
    sub_topics:
      - name: "Tunique externe"
        collection: "Tunique externe"
        keywords:
          - "corneal anatomy"
          - "sclera structure"
        clinical_scope:
          - "cornea"
          - "sclera"
      # ...

  - name: "5 - Pathologies"
    search:
      max_results: 20
    sub_topics:
      - name: "Dystrophies retiniennes"
        collection: "Dystrophies retiniennes"
        clinical_keywords:             # AND'd with each disease keyword
          - "phenotype"
          - "genotype"
        diseases:
          - name: "Retinitis pigmentosa"
            en_keywords:
              - "retinitis pigmentosa"
              - "rod-cone dystrophy"
          # ...
```

## GitHub Actions

### Secrets required

| Secret | Purpose |
|---|---|
| `ZOTERO_API_KEY` | Read/Write access to group |
| `ZOTERO_GROUP_ID` | Numeric group ID from Zotero URL |
| `OPENALEX_API_KEY` | Optional; OpenAlex API key for higher rate limit |

### Manual dispatch inputs

| Input | Type | Description |
|-------|------|-------------|
| `genes` | string | Space-separated gene symbols (empty = all from genes.yml) |
| `mode` | choice | Pipeline mode: empty (both), `genes`, `topics` |
| `categories` | string | Topic category filter (e.g., "1 - Anatomy") |

The weekly cron schedule runs both pipelines by default.

## Dependencies

- `pyzotero` -- Zotero API client
- `httpx` -- HTTP client with configurable timeouts (used by pyzotero)
- `requests` -- HGNC REST API + OpenAlex API
- `pyyaml` -- config parsing

Python 3.12+.

## Future work

- **Citation quality scoring**: OpenAlex provides `fwci` (field-weighted citation
  impact) and `citation_normalized_percentile` on work objects. These normalize
  citation counts by field and publication year, making them better signals than
  raw `cited_by_count` for ranking citation expansion candidates. When reworking
  the scoring formula, add both fields to WORK_FIELDS and integrate into
  `_expand_one_hop()`.
