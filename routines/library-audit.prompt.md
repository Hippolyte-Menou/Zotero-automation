# Library Audit Routine — cloud config (reference copy)

Create at claude.ai/code/routines (or `/schedule daily library audit`, then
`/schedule update` for the cron). This file is the source-of-truth copy of the
routine's prompt and settings; the live config lives in your claude.ai account.

## Settings
- **Model (orchestrator):** Sonnet
- **Repo:** zotero-bot (default-branch clone). Enable **Allow unrestricted branch
  pushes** (the ledger must be committed back to `gh-pages`).
- **Schedule:** cron `0 20 * * *` (daily 20:00 local).
- **Env vars:** `ZOTERO_API_KEY`, `ZOTERO_GROUP_ID`, `OPENALEX_API_KEY`.
- **Network access:** add `api.zotero.org`, `api.openalex.org`.
- **Connectors:** keep PubMed + bioRxiv; remove the rest.
- **Setup script:** `pip install -r requirements.txt` then
  `pip install "bio_toolkit @ git+https://github.com/Hippolyte-Menou/bio_toolkit@<sha>"`.

## Prompt

You are auditing the Zotero group library for misfiled papers (false positives)
and wrongly-dismissed papers (false negatives), working through the backlog. Do
NOT judge relevance yourself — delegate every judgment to subagents.

1. Pull the latest dashboard data and ledger from gh-pages:
   `git fetch origin gh-pages`
   `mkdir -p site/data data`
   `git show origin/gh-pages:site/data/near_misses.json > site/data/near_misses.json`
   `git show origin/gh-pages:data/audit_state.json > data/audit_state.json 2>/dev/null || echo '{"audited_ids":[]}' > data/audit_state.json`
   `git show origin/gh-pages:data/audit_log.json > data/audit_log.json 2>/dev/null || echo '[]' > data/audit_log.json`
2. Run `python audit_bot.py --prepare --max-items 400`. Read `audit_work/manifest.json`.
3. For each batch in `fp_batches` and `fn_batches`, dispatch a **library-screener**
   subagent, telling it the batch file path (e.g. `audit_work/batches/fp_000.json`).
   Run them in parallel.
4. Run `python audit_bot.py --collect`. For each batch in
   `audit_work/adj_manifest.json`, dispatch a **relevance-adjudicator** subagent
   with its batch path. Run them in parallel.
5. Run `python audit_bot.py --apply`. (For the FIRST run only, run
   `python audit_bot.py --apply --dry-run` instead and STOP — skip step 6. A
   dry-run trashes/rescues nothing and leaves the ledger untouched; inspect the
   intended actions in `data/audit_log.json` (records marked `"applied": false`),
   then re-run live once satisfied.)
6. (Live runs only.) Persist state back to gh-pages (mirror how
   `gene_pipeline.yml` deploys `data/` files): commit `data/audit_state.json` and
   `data/audit_log.json` to the `gh-pages` branch and push.
7. Print a summary: counts trashed / rescued / kept, with one example line each.

If any `audit_bot.py` step exits non-zero, STOP and report — do not act on
partial data.
