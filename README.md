# Gene Literature Bot

OpenAlex-powered literature search for gene-specific papers, with citation network expansion and automatic upload to a Zotero group library. Runs on GitHub Actions (no server needed).

## How it works

1. Fetches gene aliases from HGNC
2. Searches OpenAlex for articles mentioning the gene in title/abstract
3. Applies text exclusion filters, deduplicates against Zotero library (by PMID)
4. Uploads new search-found papers to Zotero (`source:search` tag)
5. Expands via citation network (one-hop refs + citations), with adaptive selectivity that raises the bar as the library grows
6. Uploads citation-discovered papers (`source:citation` tag)

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

### 3. Edit `genes.yml`

Add your genes of interest. Each gene can have custom text exclusion terms or inherit the defaults.

### 4. Run

- **Automatic**: runs every Sunday at 03:00 UTC (edit cron in `.github/workflows/genebot.yml`)
- **Manual**: go to Actions tab > Gene Literature Bot > Run workflow
- **Specific genes**: use the "genes" input field (e.g., `CRB1 PAX6`) when triggering manually

## Local usage

```bash
pip install -r requirements.txt

export ZOTERO_API_KEY="xxx"
export ZOTERO_GROUP_ID="123456"

python run.py              # all genes from genes.yml
python run.py CRB1 RHO     # specific genes only
```

## Exclusion strategy

Text exclusion filters remove papers whose title contains excluded terms (whole-word, case-insensitive). Configure in `genes.yml` at the default level or per-gene.

## Notes

- The bot is idempotent: re-running only adds newly published papers
- Zotero uploads use batch API (50 items/request) for efficiency
- Logs are uploaded as GitHub Actions artifacts (retained 30 days)
- Citation expansion uses adaptive selectivity: genes with large existing libraries require higher co-citation counts for new candidates
