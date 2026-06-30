# Library Audit Routine — cloud config (reference copy)

Create at [claude.ai/code/routines](https://claude.ai/code/routines). This file is
the source-of-truth copy of the routine's prompt and settings; the live config
lives in your claude.ai account. Reflects the config that actually works in the
research-preview cloud environment (see "Deployment gotchas" at the bottom).

## Settings

- **Model (orchestrator):** Sonnet. The token-heavy work is on the pinned
  Haiku/Sonnet subagents, so the orchestrator stays lean.
- **Repositories (two):**
  - `Hippolyte-Menou/Zotero-automation` (this repo — contains `audit_bot.py`).
  - `Hippolyte-Menou/bio_toolkit` — **required**, so the GitHub proxy authorizes
    the `bio_toolkit` install in the setup script. Without it the setup clone 403s.
- **Permissions:** leave **"Allow unrestricted git push" OFF.** The ledger is
  written to a `claude/audit-state` branch (always pushable); nothing touches a
  protected branch. Enabling this toggle currently makes routine creation fail.
- **Schedule:** cron `0 5 * * *` (daily, local time).
- **Env vars:** `ZOTERO_API_KEY` (required — the only essential secret;
  `ZOTERO_GROUP_ID` already lives in `bio_toolkit.config`). `OPENALEX_API_KEY`
  optional (higher OpenAlex rate limit only).
- **Network access (Custom):** add `api.zotero.org` and `api.openalex.org`, and
  keep the default package-manager list. (Run-time only; setup needs just the
  default allowlist.)
- **Connectors:** keep PubMed + bioRxiv (used by the adjudicator); remove the rest.
- **Setup script** (order and flags matter — see gotchas):
  ```bash
  pip install --ignore-installed setuptools wheel
  pip install --ignore-installed --prefer-binary pyzotero==1.10.0 requests==2.34.2 pyyaml==6.0.3
  pip install --no-deps "bio_toolkit @ git+https://github.com/Hippolyte-Menou/bio_toolkit@master"
  ```

## Prompt (paste into Instructions)

You are auditing the Zotero group library for misfiled papers (false positives)
and wrongly-dismissed papers (false negatives), working through the backlog. Do
NOT judge relevance yourself — delegate every judgment to the `library-screener`
and `relevance-adjudicator` subagents. The sweep ledger and audit log live on the
`claude/audit-state` branch; near-miss data is read-only from `gh-pages`. Never
push to `gh-pages` or `main`. If any `audit_bot.py` step exits non-zero, STOP and
report — do not act on partial data.

0. Move into the zotero-bot repo (the one containing audit_bot.py — the workspace
   holds two repos):
   ```
   cd "$(dirname "$(find . -maxdepth 3 -name audit_bot.py 2>/dev/null | head -1)")"
   pwd
   ```

1. Pull current data — near-misses (read-only from gh-pages) and the ledger (from
   claude/audit-state, which may not exist on the first run). Fetch each branch
   into an explicit LOCAL ref (the cloud clone is single-branch, so `origin/<b>`
   refs do not exist — use `<b>:<b>` and read from the local ref):
   ```
   git fetch origin gh-pages:gh-pages 2>/dev/null || true
   git fetch origin claude/audit-state:claude/audit-state 2>/dev/null || true
   mkdir -p site/data data
   git show gh-pages:site/data/near_misses.json > site/data/near_misses.json 2>/dev/null || echo '{"articles":[]}' > site/data/near_misses.json
   git show claude/audit-state:data/audit_state.json > data/audit_state.json 2>/dev/null || echo '{"audited_ids":[]}' > data/audit_state.json
   git show claude/audit-state:data/audit_log.json > data/audit_log.json 2>/dev/null || echo '[]' > data/audit_log.json
   ```
   Sanity check: `python -c "import json;print('near_misses', len(json.load(open('site/data/near_misses.json')).get('articles',[])))"` should be non-zero.

2. Build candidate batches: `python audit_bot.py --prepare --max-items 400`.
   Read `audit_work/manifest.json` (lists `fp_batches` and `fn_batches`).

3. For EACH batch in `fp_batches` and `fn_batches`, dispatch a `library-screener`
   subagent (subagent_type `library-screener`) with ONLY the batch file path,
   e.g. `audit_work/batches/fp_000.json`. Run them in parallel. Each writes a
   `verdicts/screen_*.json` file.

4. Build the second-pass batches: `python audit_bot.py --collect`. Read
   `audit_work/adj_manifest.json`; for EACH batch in `adj_batches`, dispatch a
   `relevance-adjudicator` subagent with its batch path. Run them in parallel.

5. Apply the decisions: `python audit_bot.py --apply`. This trashes confirmed
   off-topic items (recoverable Zotero trash; requires BOTH subagents to concur)
   and rescues confirmed relevant ones, then advances the ledger.

6. Save the updated ledger + log to the claude/audit-state branch (separate
   worktree so it doesn't disturb the main checkout; files are gitignored, so
   force-add):
   ```
   git fetch origin claude/audit-state:claude/audit-state 2>/dev/null || true
   git worktree add -f /tmp/state claude/audit-state 2>/dev/null || git worktree add -f -b claude/audit-state /tmp/state
   mkdir -p /tmp/state/data
   cp data/audit_state.json data/audit_log.json /tmp/state/data/
   git -C /tmp/state add -f data/audit_state.json data/audit_log.json
   git -C /tmp/state commit -m "audit: update ledger + log" || echo "nothing to commit"
   git -C /tmp/state push -u origin claude/audit-state
   git worktree remove --force /tmp/state
   ```

7. Print a summary from the --apply output: counts trashed / rescued / kept (and
   failed_trash / failed_rescue), with one example line each for a trashed and a
   rescued paper.

## First run — verify before going live

The prompt above is always live. Before trusting the daily schedule, do ONE manual
dry-run: temporarily change step 5 to `python audit_bot.py --apply --dry-run` and
delete step 6, click **Run now**, then inspect `data/audit_log.json` (records
marked `"applied": false`) to see exactly what it would trash/rescue. A dry-run
mutates nothing and leaves the ledger untouched, so once you restore step 5/6 the
first live run acts on exactly what you previewed. Consider a smaller first
`--max-items` (e.g. `--prepare --max-items 50`) to limit blast radius, then raise
it back to 400. `--batch-size` (default 20) controls items per screener subagent;
drop to 10 for crisper per-item judgments.

## Deployment gotchas (lessons from setting this up)

- **Setup script / Debian system Python.** The cloud env is system Python with
  OS-managed packages. `pip install -r requirements.txt` fails (the repo isn't in
  the setup CWD — setup runs before the clone), and upgrading/installing pins
  fails trying to uninstall OS-managed packages ("Cannot uninstall … RECORD file
  not found"). Fixes baked into the setup script above: install deps by name (not
  `-r`), `--ignore-installed` (lay down fresh shadowing copies, never uninstall),
  `--ignore-installed setuptools wheel` first (Debian's patched setuptools can't
  build `pyzotero`'s ancient `sgmllib3k`/`bibtexparser` deps — `install_layout`
  error), and `--prefer-binary`.
- **bio_toolkit 403.** Routines clone via a GitHub proxy that only authorizes the
  routine's selected repos. `bio_toolkit` must be added as a second repo or its
  `git+https` install 403s.
- **Single-branch clone.** `git fetch origin <branch>` does NOT create
  `refs/remotes/origin/<branch>`; reading via `origin/<branch>` silently fails and
  the `|| echo` fallback yields empty data (this caused near_misses = 0). Fetch
  into an explicit local ref (`<branch>:<branch>`) and read from it.
- **No gh-pages push.** Persisting the ledger to gh-pages needs "unrestricted git
  push", which breaks creation — so the ledger lives on `claude/audit-state`
  instead. Nothing else reads it, so there's no downside.
