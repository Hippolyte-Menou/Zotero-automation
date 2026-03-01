# Gene & Topic Literature Bot

OpenAlex-powered literature search with two complementary pipelines -- gene-specific papers (with citation network expansion) and topic-based ophthalmology literature (anatomy, embryology, physiology, examinations, pathologies). Automatic upload to a Zotero group library. Runs on GitHub Actions (no server needed).

## How it works

### Gene pipeline

For each gene, the pipeline runs three passes:

#### Pass 1 -- Search (bootstrap + periodic re-search)

On first encounter (empty collection), searches OpenAlex using a boolean query:

```
(GENE OR alias1 OR alias2) AND ("disease A" OR "disease B" OR ...)
```

- Gene terms come from HGNC aliases (with per-gene `blocked_aliases` to suppress noisy aliases)
- Disease terms come from the `tags` field in `genes.yml` (slug `retinitis-pigmentosa` -> `"retinitis pigmentosa"`)
- Filtered to `type:article, has_pmid:true`
- Capped at `search.max_results` results (default 25)
- Papers passing text exclusion are uploaded with `source:search` tag

Once a gene has papers, search is skipped unless `re_search_interval_weeks` has elapsed since the last search date (tracked per gene in the citation cache). This catches papers that match the query but are not reachable via citation expansion.

#### Pass 2 -- Citation network expansion

Expands from the seed set (existing Zotero papers, or search results) via two hops:

**Hop 1**: For each seed, collect backward references (cached per seed) and forward citations. Score every candidate:

```
effective_score = co_citations + bib_coupling_bonus + recency_bonus

  co_citations        -- how many seeds reference or cite this candidate
  bib_coupling_bonus  -- min(shared_references_with_seed_set, 3)
  recency_bonus       -- max(0, 3 - (current_year - paper_year))
```

Candidates must clear an adaptive threshold:

```
adaptive_min_co = min(max_min_co, max(min_co_citations, floor(log2(library_size))))
```

| library_size | adaptive_min_co (with defaults: min=1, max=6) |
|---|---|
| 1-3          | 1 |
| 4-7          | 2 |
| 8-15         | 3 |
| 16-31        | 4 |
| 32-63        | 5 |
| 64+          | 6 (capped) |

After filtering, full metadata is fetched for surviving candidates. A mention-term filter then removes any paper that does not mention the gene symbol or an alias in its title or abstract.

**Hop 2**: Top `hop2_top_n` (default 10) candidates from hop 1 become new seeds; the same scoring and filtering logic repeats.

Surviving candidates are text-filtered, deduplicated, and uploaded with `source:citation` tag.

#### Pass 3 -- Recent papers

A separate OpenAlex search filtered to the current year:

```
(GENE OR aliases) AND (disease terms) AND from_publication_date:YYYY-01-01
```

Capped at `search.recent_max_results` (default 10). Bypasses the adaptive citation threshold entirely -- new papers cannot yet accumulate co-citations or bibliographic coupling, so they are fetched directly. Text-filtered, deduplicated, uploaded with `source:recent` tag.

### Topic pipeline

For each sub-topic defined in `topics.yml`, the pipeline searches OpenAlex using one of two modes:

#### Keyword OR mode (categories 1-4: Anatomy, Embryology, Physiology, Examinations)

All keywords are OR'd into a single query, scoped to the ophthalmology subfield and optional topic IDs:

```
title_and_abstract.search: "corneal anatomy" OR "sclera structure" OR ...
primary_topic.subfield.id: 2731
type: review|article
```

#### Disease+clinical AND mode (category 5: Pathologies)

For each disease keyword, AND with clinical keywords, then merge results across diseases:

```
title_and_abstract.search: "retinitis pigmentosa" AND ("phenotype" OR "genotype" OR ...)
primary_topic.subfield.id: 2731
```

Per-disease result cap = `max_results / len(diseases)` (floor of 5).

#### Shared steps

After search:
1. Apply text exclusion and MeSH exclusion filters
2. Deduplicate against shared PMID set (cross-pipeline); DOI used as secondary dedup key for records missing PMIDs
3. Upload with `source:search` tag, tagged with category and sub-topic names
4. Citation expansion (if enabled) with mention-term filtering
5. Recent papers pass for current-year publications
6. Link related items via Zotero dc:relation

Collections are created as nested hierarchies mirroring the vault structure (e.g., "1 - Anatomy" / "Tunique externe").

---

## Setup

### 1. Get API credentials

| Credential | Where |
|---|---|
| **Zotero API key** | https://www.zotero.org/settings/keys/new -- enable Read/Write on your group |
| **Zotero group ID** | Numeric ID from your group URL: `zotero.org/groups/123456/...` |
| **OpenAlex API key** | Optional. https://openalex.org -- higher rate limit |

### 2. Configure GitHub repository

1. Fork or push this repo to GitHub
2. Go to **Settings > Secrets and variables > Actions**
3. Add these secrets:
   - `ZOTERO_API_KEY`
   - `ZOTERO_GROUP_ID`
   - `OPENALEX_API_KEY` (optional, recommended)

### 3. Edit config files

#### `genes.yml` -- gene pipeline

Add your genes of interest. Each gene can have disease keyword tags and optional custom text exclusion terms.

```yaml
search:
  max_results: 25          # cap search results per gene (bootstrap only)
  recent_max_results: 10   # cap recent-papers pass per gene
  re_search_interval_weeks: 8   # re-run search pass every N weeks (0 = disabled)

citation_expansion:
  max_seed_papers: 100     # top N most-cited seeds used for expansion
  min_co_citations: 1      # floor for adaptive threshold
  max_min_co: 6            # ceiling for adaptive threshold
  max_hops: 2
  hop2_top_n: 10

genes:
  - symbol: ABCA4
    collection: ABCA4
    tags:
      - stargardt-disease
      - retinitis-pigmentosa
    exclude_text:           # optional per-gene override (inherits defaults otherwise)
      - "some term"
    blocked_aliases:        # suppress HGNC aliases that are common words or collide
      - "ABC"
```

#### `topics.yml` -- topic pipeline

Define categories and sub-topics with keywords. Settings inherit: sub-topic > category > global. Category aliases for the CLI are defined in `topics.yml` under each category's `alias` field.

```yaml
search:
  max_results: 30
  recent_max_results: 10

openalex_scoping:
  subfield_id: 2731        # ophthalmology
  type_filter: "review|article"

categories:
  - name: "1 - Anatomy"
    alias: anatomy
    sub_topics:
      - name: "Tunique externe"
        collection: "Tunique externe"
        keywords:
          - "corneal anatomy"
          - "sclera structure"

  - name: "5 - Pathologies"
    alias: pathologies
    sub_topics:
      - name: "Dystrophies retiniennes"
        collection: "Dystrophies retiniennes"
        clinical_keywords:
          - "phenotype"
          - "genotype"
        diseases:
          - name: "Retinitis pigmentosa"
            en_keywords:
              - "retinitis pigmentosa"
              - "rod-cone dystrophy"
```

### 4. Run

- **Automatic**: runs every Saturday at 22:00 UTC (edit cron in `.github/workflows/genebot.yml`)
- **Manual**: go to Actions tab > Gene & Topic Literature Bot > Run workflow
  - `mode`: `both` (default, both pipelines), `genes`, or `topics`
  - `genes`: space-separated gene symbols (e.g., `CRB1 PAX6`)
  - `categories`: topic category filter (e.g., `1 - Anatomy`)

The orphan detection workflow runs automatically every Wednesday and can also be triggered manually from Actions tab > Inverse Bot.

## Local usage

```bash
pip install -r requirements.txt

export ZOTERO_API_KEY="xxx"
export ZOTERO_GROUP_ID="123456"

python run.py                          # both pipelines (default)
python run.py --genes                  # all genes from genes.yml
python run.py --genes CRB1 RHO         # specific genes only
python run.py --topics                 # all topic categories
python run.py --topics anatomy         # specific category (alias)
python run.py --topics "1 - Anatomy"   # specific category (full name)
```

### Category aliases

Aliases are defined in `topics.yml`. Default aliases:

| Alias | Resolves to |
|-------|-------------|
| `anatomy` | `1 - Anatomy` |
| `embryology` | `2 - Embryology` |
| `physiology` | `3 - Physiology` |
| `examinations` | `4 - Examinations` |
| `pathologies` | `5 - Pathologies` |

## Notes

- Idempotent: re-running only adds newly published papers (dedup by PMID + DOI)
- Orphan detection: a separate Wednesday workflow (`inverse_bot.py`) scores every library paper for internal citation centrality and flags isolated papers (no backward/forward/bib-coupling links to any other library paper) in the dashboard Review tab
- Cross-pipeline dedup: shared PMID/DOI set prevents the same paper appearing in both gene and topic collections
- Incremental: once a gene has papers in Zotero, search is skipped until `re_search_interval_weeks` elapses
- Text exclusion (title + abstract, whole-word regex) removes off-topic papers (cancer, tumor, etc.)
- MeSH exclusion removes papers tagged with excluded MeSH descriptors (Neoplasms, Diabetes Mellitus, etc.)
- Provenance tagging: `source:search`, `source:citation`, `source:recent`, `source:rescue`
- Zotero uploads use batch API (50 items/request) with 60s timeout and retry (stops after first retry on timeout to avoid duplicates)
- Post-upload verification: re-fetches collection PMIDs after each batch and warns about silently lost papers
- OpenAlex API: separate retry budgets for errors (3 attempts) and rate limits (5 attempts with exponential backoff)
- HGNC alias resolution retries transient failures (3 attempts)
- Citation candidates pre-filtered to co_citations >= 1 before PMID resolution (reduces API calls)
- Backward references are cached per seed in the citation cache to avoid redundant API calls on stable papers
- Failed API request count logged at end of run for visibility
- Per-run metrics (found/new/uploaded/failed per gene/topic, API errors) appended to `data/run_history.json` and written to GitHub Actions job summary; two records per full run (one per pipeline job)
- Logs are uploaded as separate GitHub Actions artifacts per pipeline (`run-logs-genes-*`, `run-logs-topics-*`), retained 30 days

---

## Near-Miss Articles Dashboard

Every pipeline run generates a JSON log of articles that were considered but ultimately rejected. These near-misses are deployed as a static GitHub Pages dashboard so you can review what was filtered out and rescue interesting papers.

### What gets logged

| Rejection reason | When | Details captured |
|---|---|---|
| `score_below_threshold` | Citation expansion: candidate scored below adaptive threshold | co-citations, bib coupling, recency bonus, effective score, threshold, direction |
| `text_exclusion` | Title/abstract matched a blocked term (cancer, tumor, mouse model, ...) | matched term, highlighted in title and abstract |
| `mesh_exclusion` | MeSH descriptors matched a blocked descriptor (Neoplasms, Diabetes, ...) | matched MeSH descriptor, shown as a tag badge |
| `mention_filter` | Citation candidate passed scoring but gene/keyword not mentioned in title or abstract | co-citations, bib coupling, recency bonus, effective score, direction |

For `score_below_threshold`, metadata is fetched for the top 50 candidates per hop (ranked by effective score) to keep API calls bounded.

### Dashboard features

**Navigation and filtering**
- **Sidebar navigation**: collapsible tree following vault hierarchy (1-Anatomy through 6-Genes), with article counts per subcollection
- **Search**: real-time text filter on subcollection names; A-Z letter filter for genes
- **Reason filter**: dropdown to show all reasons or a specific one (defaults to score_below_threshold)
- **Sortable columns**: clickable headers (Article, Reason, Score, Cited, Year) with toggle asc/desc and visual sort arrows
- **Recurring filter**: checkbox to show only articles seen in multiple pipeline runs

**Article display**
- **Article table**: title (linked to PubMed), authors, journal, year, citation count, color-coded rejection badge, score breakdown for citation candidates
- **Score progress bars**: for score-below-threshold articles, a visual bar showing `effective_score / threshold` ratio with red (< 50%) / amber (50-80%) / green (> 80%) gradient
- **"Closest to threshold" sort**: preset that orders articles by how close they came to passing, auto-filtering to score-based rejections
- **Exclusion trigger highlighting**: for text-excluded articles, the matched term is highlighted in both title and abstract; for MeSH-excluded articles, the triggering descriptor is shown as a colored tag badge
- **Expandable abstracts**: click to toggle per article
- **Pagination**: 50 articles per page

**Analysis panels** (toggle via header buttons)
- **Stats panel**: total rejections, breakdown by reason as horizontal bars, top 5 subcollections by article count, pipeline run count
- **History panel**: per-run metrics table (date, pipelines run, papers found/new/uploaded/failed, OpenAlex API errors) loaded from `data/run_history.json`
- **Recent additions panel**: papers uploaded in the last 4 weeks grouped by subcollection, with source-tag badges (search/citation/recent/rescue) and PubMed links, loaded from `data/recent_additions.json`

**Cumulative tracking**
- Merges new rejections with existing data across runs; tracks `first_seen`, `last_seen`, and `seen_count` per article
- Recurring articles are flagged with a badge showing how many runs they have appeared in
- Cross-subcollection view: "Shared Near-Misses" entry in sidebar showing articles rejected in 2+ genes/topics

**Rescue queue**
- Per-article "Rescue" button saves articles to a localStorage queue
- "Download rescue_queue.json" exports the queue for bot pickup
- On the next pipeline run, `run.py` reads `data/rescue_queue.json`, looks up each article on OpenAlex, uploads it to the target Zotero collection with `source:rescue` tag, and writes back any failed entries for retry
- Rescued articles appear in the Recent additions panel on the next dashboard deployment

### Review tab (orphan detection)

A companion tab in the same dashboard shows papers flagged by the inverse bot as orphans -- library papers with zero internal citation centrality (no backward reference, forward citation, or bibliographic coupling link connects them to any other library paper).

**How it works**: `inverse_bot.py` builds a citation adjacency graph from all Zotero library papers using OpenAlex data, computes a centrality score for each paper, and flags those with score = 0. Results are written to `site/data/flagged_papers.json` and deployed to gh-pages.

**Dashboard features**:
- Same two-tier collapsible sidebar as Near Misses: category -> subcollection, A-Z letter strip for the Genes category, "Shared Flagged" entry at top for papers in multiple collections
- Article table with title, authors, journal, year, and source-tag badge
- Per-article "Dismiss" button: saves the PMID to localStorage `inverseBotsWhitelist`, hiding it from the list
- "Download whitelist" exports `inverse_bot_whitelist.json` for bot pickup; the inverse bot skips whitelisted papers on subsequent runs
- Dark mode, mobile responsive, accessible (same as Near Misses)

**Runs on a separate schedule**: Wednesday cron (`.github/workflows/inverse_bot.yml`), independent from the weekly genebot workflow.

**Threshold tuning**
- Sidebar widget to adjust the adaptive threshold and preview which articles would have passed
- Rescued articles highlighted with a "PASS" badge in the table
- Reset button restores the current threshold

**Interface**
- **BibTeX export**: select articles via checkboxes, export as `.bib` file for manual Zotero import (LaTeX special characters escaped, DOI normalized)
- **Dark mode**: defaults to system preference via `prefers-color-scheme`, manual toggle persists via localStorage; panel open states preserved on toggle
- **Mobile responsive**: hamburger menu toggle for sidebar on screens < 768px
- **Accessible**: ARIA labels on all interactive elements, keyboard navigation (Enter/Space) on sidebar tree items and tuning panel header

### How it deploys

The GitHub Actions workflow runs 3 sequential jobs, each with its own 360-minute timeout:

```
gh-pages baseline --> [run-genes] --artifact--> [run-topics] --artifact--> [deploy-dashboard]
```

1. **run-genes**: fetches baseline data from gh-pages, runs the gene pipeline, uploads `data/` as an artifact. Skipped when `mode=topics`.
2. **run-topics**: fetches gh-pages baseline, downloads the genes artifact (overwrites baseline with genes' output), runs the topic pipeline, uploads the updated `data/` artifact. Skipped when `mode=genes`. Does not run if the genes job failed.
3. **deploy-dashboard**: downloads the final artifact, copies data files into `site/data/`, deploys `site/` to gh-pages with `keep_files: true`.

`keep_files: true` merges into the gh-pages branch instead of replacing it. This means partial runs never wipe complete data -- only the files present in the artifact are overwritten. This is safe because all data files are cumulative (near-misses merge, run history appends, recent additions deduplicate).

The dashboard is then accessible at `https://<username>.github.io/Zotero-automation/`.

**First-time setup**:
1. Go to **Settings > Actions > General > Workflow permissions** and enable **Read and write permissions**
2. Run the workflow once (manual trigger or wait for schedule)
3. Go to **Settings > Pages**, set Source to **Deploy from a branch**, Branch to **gh-pages** / **/ (root)**
4. The site URL will appear under Pages settings after a few minutes

### Local preview

Generate synthetic test data and serve locally:

```bash
python generate_test_data.py          # creates near-misses + flagged papers + run history + recent additions + rescue queue
python -m http.server 8000 -d site    # open http://localhost:8000
```

`generate_test_data.py` writes five files to `site/data/`:

| File | Content |
|---|---|
| `near_misses.json` | ~990 synthetic rejected articles with cumulative tracking fields |
| `flagged_papers.json` | ~150 synthetic orphan papers with `hierarchy` + `category`/`subcollection` fields |
| `run_history.json` | 8 synthetic pipeline runs over the last 8 weeks |
| `recent_additions.json` | ~90 synthetic uploaded papers from the last 6 weeks |
| `rescue_queue.json` | 5 pre-populated rescue queue entries |

### Files

| File | Purpose |
|---|---|
| `inverse_bot.py` | Orphan detection: citation centrality scoring, flags isolated library papers |
| `genebot/rejection_log.py` | `RejectionLog` class -- accumulates rejected articles, handles cumulative merge with previous data |
| `site/index.html` | Complete single-page dashboard with Near Misses and Review tabs (HTML + CSS + JS, no dependencies) |
| `generate_test_data.py` | Generates realistic synthetic data for all five dashboard data files |
| `data/near_misses.json` | Pipeline output (gitignored, generated at runtime) |
| `data/flagged_papers.json` | Orphan detection output (gitignored, generated at runtime) |
| `data/inverse_bot_whitelist.json` | Dismissed orphan PMIDs (gitignored, generated by dashboard export) |
| `data/run_history.json` | Cumulative per-run metrics (gitignored, generated at runtime) |
| `data/recent_additions.json` | Uploaded papers tracker (gitignored, generated at runtime) |
| `data/rescue_queue.json` | Rescued articles pending bot pickup (gitignored, generated by dashboard export) |
| `site/data/*.json` | Copies for gh-pages deployment (gitignored, generated by CI) |
