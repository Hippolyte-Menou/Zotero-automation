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
and `relevance-adjudicator` subagents. The sweep ledger, audit log, and per-gene
feedback live on the `claude/audit-state` branch; near-miss data is read-only
from `gh-pages`. Never push to `gh-pages` or `main`. If any `audit_bot.py` step
exits non-zero, STOP and report — do not act on partial data.

Shell state does NOT persist between steps, so every command block below begins
by cd-ing into the repo. Dispatch discipline: when a step says dispatch
subagents, send them ALL in a single message so they run in parallel, then wait
for every one to finish before the next step — do not poll in a loop.

0. Locate the repo (the workspace holds two; we want the one with audit_bot.py):
   ```
   cd "$(dirname "$(find . -maxdepth 4 -name audit_bot.py 2>/dev/null | head -1)")" && pwd
   ```

1. Pull current data, then HARD-STOP if the near-miss read came back empty.
   Near-misses are read-only from `gh-pages`, published at **`data/near_misses.json`**
   on that branch (the deploy publishes `./site` as the branch root, so it is NOT
   under `site/data/` there). The ledger/log/feedback come from
   `claude/audit-state` (may not exist on the first run). The cloud clone is
   single-branch, so fetch each branch into an explicit LOCAL ref (`<b>:<b>`) and
   read from that ref — `origin/<b>` refs do not exist:
   ```
   cd "$(dirname "$(find . -maxdepth 4 -name audit_bot.py 2>/dev/null | head -1)")"
   git fetch origin gh-pages:gh-pages 2>/dev/null || true
   git fetch origin claude/audit-state:claude/audit-state 2>/dev/null || true
   mkdir -p site/data data
   git show gh-pages:data/near_misses.json > site/data/near_misses.json 2>/dev/null || echo '{"articles":[]}' > site/data/near_misses.json
   git show claude/audit-state:data/audit_state.json > data/audit_state.json 2>/dev/null || echo '{"audited_ids":[]}' > data/audit_state.json
   git show claude/audit-state:data/audit_log.json > data/audit_log.json 2>/dev/null || echo '[]' > data/audit_log.json
   git show claude/audit-state:data/audit_feedback.json > data/audit_feedback.json 2>/dev/null || echo '{}' > data/audit_feedback.json
   python - <<'PY'
   import json, sys
   n = len(json.load(open("site/data/near_misses.json")).get("articles", []))
   print("near_misses", n)
   sys.exit(1 if n == 0 else 0)
   PY
   ```
   If that block exits non-zero (near_misses == 0), the `gh-pages` read failed —
   STOP and report. Auto-acting on a broken read would skip every rescue and
   still trash on partial context.

2. Build candidate batches:
   ```
   cd "$(dirname "$(find . -maxdepth 4 -name audit_bot.py 2>/dev/null | head -1)")"
   python audit_bot.py --prepare --max-items 400
   ```
   It fetches the library ONCE, prints the absolute work dir, and writes
   `audit_work/manifest.json` (lists `fp_batches` and `fn_batches`). Screener
   batches hold 10 items each (`--batch-size`, default 10); adjudication batches
   hold 20 (`--adj-batch-size`).

3. For EACH batch name in `fp_batches` and `fn_batches`, dispatch a
   `library-screener` subagent (subagent_type `library-screener`) with ONLY the
   batch file path (e.g. `audit_work/batches/fp_000.json`). Dispatch them all in
   one message; run in parallel. Each batch file carries a `verdict_out` path and
   the agent writes its verdicts there — no file-moving is needed.

4. Build the second-pass batches:
   ```
   cd "$(dirname "$(find . -maxdepth 4 -name audit_bot.py 2>/dev/null | head -1)")"
   python audit_bot.py --collect
   ```
   Read `audit_work/adj_manifest.json`; for EACH batch in `adj_batches`, dispatch
   a `relevance-adjudicator` subagent with its batch path (e.g.
   `audit_work/batches/adj_000.json`). All in one message, in parallel.

5. Apply the decisions:
   ```
   cd "$(dirname "$(find . -maxdepth 4 -name audit_bot.py 2>/dev/null | head -1)")"
   python audit_bot.py --apply
   ```
   This trashes confirmed off-topic items and rescues confirmed relevant ones —
   both require BOTH subagents to concur (symmetric gate) — then advances the
   ledger and updates `data/audit_feedback.json`. Trash is recoverable Zotero
   trash. It reuses the dedup baseline cached in step 2 (no extra library fetch).

6. Save the updated ledger + log + feedback to the `claude/audit-state` branch
   (separate worktree so it doesn't disturb the main checkout; files are
   gitignored, so force-add):
   ```
   cd "$(dirname "$(find . -maxdepth 4 -name audit_bot.py 2>/dev/null | head -1)")"
   git fetch origin claude/audit-state:claude/audit-state 2>/dev/null || true
   git worktree add -f /tmp/state claude/audit-state 2>/dev/null || git worktree add -f -b claude/audit-state /tmp/state
   mkdir -p /tmp/state/data
   cp data/audit_state.json data/audit_log.json data/audit_feedback.json /tmp/state/data/
   git -C /tmp/state add -f data/audit_state.json data/audit_log.json data/audit_feedback.json
   git -C /tmp/state commit -m "audit: update ledger + log + feedback" || echo "nothing to commit"
   git -C /tmp/state push -u origin claude/audit-state
   git worktree remove --force /tmp/state
   ```

7. Print a summary from the `--apply` output: counts trashed / rescued / kept
   (and failed_trash / failed_rescue), with one example line each for a trashed
   and a rescued paper. Then surface the upstream signal from
   `data/audit_feedback.json` — the genes with the most cumulative trashes
   (candidates for `blocked_aliases` in genes.yml) and the most rescues (genes
   whose scoring threshold is too strict):
   ```
   cd "$(dirname "$(find . -maxdepth 4 -name audit_bot.py 2>/dev/null | head -1)")"
   python - <<'PY'
   import json
   g = json.load(open("data/audit_feedback.json")).get("genes", {})
   t = sorted(g.items(), key=lambda kv: -kv[1]["trashed"])[:8]
   r = sorted(g.items(), key=lambda kv: -kv[1]["rescued"])[:8]
   print("top trashed (alias-collision candidates):", [(k, v["trashed"]) for k, v in t if v["trashed"]])
   print("top rescued (threshold-too-strict candidates):", [(k, v["rescued"]) for k, v in r if v["rescued"]])
   PY
   ```

## First run — verify before going live

The prompt above is always live. Before trusting the daily schedule, do ONE manual
dry-run: temporarily change step 5 to `python audit_bot.py --apply --dry-run` and
delete step 6, click **Run now**, then inspect `data/audit_log.json` (records
marked `"applied": false`) to see exactly what it would trash/rescue. A dry-run
mutates nothing and leaves the ledger + feedback untouched, so once you restore
step 5/6 the first live run acts on exactly what you previewed. Consider a smaller
first `--max-items` (e.g. `--prepare --max-items 50`) to limit blast radius, then
raise it back to 400. Screener batches default to `--batch-size 10` (crisper Haiku
judgments); adjudication uses `--adj-batch-size 20`.

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
  the `|| echo` fallback yields empty data. Fetch into an explicit local ref
  (`<branch>:<branch>`) and read from it.
- **near_misses lives at `data/near_misses.json` on gh-pages, not `site/data/`.**
  The deploy publishes `./site` as the branch root, so `site/data/near_misses.json`
  becomes `data/near_misses.json` on the branch. Reading the `site/data/` path
  returns empty → 0 near-misses → zero rescues (silent, since the `|| echo`
  fallback masks it). Step 1's hard-stop now catches this.
- **Verdict files are found without manual moves.** Each batch carries an absolute
  `verdict_out` under `audit_work/verdicts/`, and `load_verdicts` also reads a
  fallback `./verdicts` — so subagent verdicts are picked up wherever they land.
  (Earlier runs had to hand-move `verdicts/*` into `audit_work/verdicts/` each
  time, or the sweep silently judged nothing and never advanced.)
- **One library fetch per sweep.** `--prepare` derives the dedup baseline from the
  single full-library fetch and caches it (`audit_work/dedup_baseline.json`);
  `--apply` reuses it. The ~20k-item library is fetched once, not three times.
- **CWD-independent.** `audit_bot.py` anchors its default paths to its own
  directory, and every step above re-cd's into the repo, so a step that starts in
  the wrong directory can no longer scatter `audit_work/` or read an empty file.
- **No gh-pages push.** Persisting the ledger to gh-pages needs "unrestricted git
  push", which breaks creation — so the ledger/log/feedback live on
  `claude/audit-state` instead. Nothing else reads them, so there's no downside.
