# Gene Literature Bot

Exhaustive PubMed search for gene-specific literature, with automatic deduplication and upload to a Zotero group library. Runs on GitHub Actions (no server needed).

## How it works

1. Fetches gene aliases from HGNC
2. Queries PubMed for all papers mentioning the gene (with MeSH + free-text exclusion filters)
3. Deduplicates against your existing Zotero group library (by PMID)
4. Uploads new papers to Zotero, organized by gene collection

## Setup

### 1. Get API credentials

| Credential | Where |
|---|---|
| **Zotero API key** | https://www.zotero.org/settings/keys/new -- enable Read/Write on your group |
| **Zotero group ID** | Numeric ID from your group URL: `zotero.org/groups/123456/...` |
| **NCBI email** | Any email (required by Entrez policy) |
| **NCBI API key** | Optional. https://www.ncbi.nlm.nih.gov/account/ > Settings > API Key |

### 2. Configure GitHub repository

1. Fork or push this repo to GitHub
2. Go to **Settings > Secrets and variables > Actions**
3. Add these secrets:
   - `NCBI_EMAIL`
   - `NCBI_API_KEY` (optional, increases rate limit)
   - `ZOTERO_API_KEY`
   - `ZOTERO_GROUP_ID`

### 3. Edit `genes.yml`

Add your genes of interest. Each gene can have custom exclusion terms or inherit the defaults.

### 4. Run

- **Automatic**: runs every Sunday at 03:00 UTC (edit cron in `.github/workflows/genebot.yml`)
- **Manual**: go to Actions tab > Gene Literature Bot > Run workflow
- **Specific genes**: use the "genes" input field (e.g., `CRB1 PAX6`) when triggering manually

## Local usage

```bash
pip install -r requirements.txt

export NCBI_EMAIL="you@example.com"
export ZOTERO_API_KEY="xxx"
export ZOTERO_GROUP_ID="123456"

python run.py              # all genes from genes.yml
python run.py CRB1 RHO     # specific genes only
```

## Exclusion strategy

Two layers of filtering:

1. **MeSH exclusion** (at query level): precise, but newly published papers may not have MeSH terms yet
2. **Free-text exclusion** (post-hoc): catches unindexed papers by scanning title + abstract

Configure both in `genes.yml`.

## Notes

- The bot is idempotent: re-running only adds newly published papers
- Zotero uploads use batch API (50 items/request) for efficiency
- Logs are uploaded as GitHub Actions artifacts (retained 30 days)
- For genes with ambiguous symbols (e.g., NOT, WAS), consider restricting to `[tiab]` search fields
