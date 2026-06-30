# Library Audit Routine — design

- **Date:** 2026-06-30
- **Status:** Approved (design), pending spec review → implementation plan
- **Project:** zotero-bot
- **Author:** brainstormed with Claude

## 1. Problem

The two pipelines (`run.py --genes` / `--topics`) optimise recall, so two error
classes accumulate in the shared Zotero group library:

- **False positives** — off-topic papers that the citation/recent passes filed
  under a gene or topic they do not belong to. These are visible right now in
  `site/data/recent_additions.json`, e.g. *"Neurosyphilis presenting as ocular
  motor nerve palsy"* filed under **ACO2**, *"Predictors of adult ICU mortality
  in Ethiopia"* under **FBN1**, *"hippocampal engagement … parietal TMS"* under
  **PRPH2**. All judgeable as wrong from the **title alone**.
- **False negatives** — genuinely relevant papers that were rejected and logged
  as near-misses (`site/data/near_misses.json`), mostly by `mention_filter` or
  `score_below_threshold`.

We want a **Claude Code Routine** (cloud, scheduled) that periodically
self-corrects both classes by acting **directly on the Zotero API**, using
**subagents pinned to Haiku and Sonnet** to keep token cost low.

## 2. Goals / non-goals

**Goals**
- Autonomously **trash** recently-added off-topic items and **rescue**
  recently-dismissed relevant items, via the Zotero API.
- Concentrate token spend on **Haiku (bulk) + Sonnet (adjudication)** subagents;
  keep the orchestrator lean.
- **Reuse existing code** for both actions (`audit_data/trash_items.py`'s
  `delete_item` trash; `run.py:process_rescue_queue()` for rescue).
- Bound per-run work by **recent activity only**.

**Non-goals (this iteration)**
- No human triage queue / dashboard tab. (The dashboard already exists for
  near-misses; surfacing audit actions there is a possible later iteration —
  see §12.)
- No full-library or rotating back-catalogue sweep. (Later iteration.)
- No per-run caps (explicit user decision).

## 3. Fixed decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Delivery vehicle | Cloud **Routine** (only path giving model-pinned subagents on a schedule) |
| Action policy | **Auto-act** on both directions, directly via Zotero API |
| Scope per run | **Recent activity only** (rolling window) |
| Output surface | **None** — it corrects the library directly (session transcript + audit log are the record) |
| Verification topology | **A — asymmetric two-tier** (see §5) |
| Trash / rescue code | **Reuse existing** (`delete_item`; `process_rescue_queue`) |
| Per-run caps | **None** |
| Cadence | **Weekly, Sunday evening** (after the inverse bot); adjustable via `/schedule update` |
| Orchestrator model | **Sonnet** |

## 4. Architecture (one scheduled cloud run)

```
Routine session  (orchestrator model: Sonnet)
  setup: clone zotero-bot · pip install -r requirements.txt + bio_toolkit · creds from env
  │
  1. PREPARE   python audit_bot.py --prepare --window-days 8
  │     • git-read near_misses.json + recent_additions.json from the gh-pages branch
  │     • FP candidates = additions with uploaded_at in window, still in the active library,
  │       resolved PMID/DOI -> Zotero item key (ZoteroGroupClient.get_existing_items)
  │     • FN candidates = near_misses with first_seen in window, not already in the library
  │     • write batch files  batches/fp_*.json , batches/fn_*.json  + manifest.json
  │
  2. SCREEN    fan out  library-screener  (model: HAIKU), one per batch     <-- bulk
  │     reads its batch file, writes verdicts/screen_*.json
  │     FP verdict: on_topic | off_topic | uncertain        (+confidence, reason)
  │     FN verdict: relevant | correctly_rejected | uncertain (+confidence, reason)
  │
  3. ADJUDICATE python audit_bot.py --collect   (builds adjudication batches from flagged/uncertain)
  │     fan out  relevance-adjudicator (model: SONNET), one per batch       <-- subset only
  │     may use PubMed / bioRxiv connectors for context; writes verdicts/adj_*.json
  │
  4. APPLY     python audit_bot.py --apply
  │     • TRASH  iff screener=off_topic AND adjudicator=off_topic   (2-model concurrence)
  │            -> ZoteroGroupClient.trash_items(keys)   [reuses delete_item — recoverable]
  │     • RESCUE iff adjudicator=relevant                            (single Sonnet vote)
  │            -> build rescue_entries -> process_rescue_queue(...)  [reused unchanged]
  │     • write data/audit_log.json (cumulative)
  │
  5. RECORD    git-commit audit_log.json to gh-pages (keep_files-style); print summary
```

The orchestrator never holds article text: `--prepare` writes batches to disk,
subagents read their own batch by path and write verdict files, and `--apply`
reads verdict files. Token spend lands on the pinned subagents.

## 5. Verification topology (A — asymmetric two-tier)

Risk is asymmetric: **rescue is reversible** (a wrong rescue is just another FP,
caught next run), **trash is destructive** on a shared library. Gates differ:

- **Rescue (FN):** Haiku pre-filters (`correctly_rejected` dropped cheaply);
  Sonnet adjudicates the rest; rescue on a **single** Sonnet `relevant` verdict.
- **Trash (FP):** Haiku screens; only `off_topic`/`uncertain` go to Sonnet;
  **trash only when Haiku said `off_topic` AND Sonnet says `off_topic`**
  (two independent models concur). A screener `uncertain` that Sonnet later
  calls `off_topic` is **kept** and logged (one vote ≠ concurrence). This is
  deliberately conservative; relaxing to "Sonnet decides" is a one-line change
  if it proves too cautious.

Everything stays within the Haiku/Sonnet tier the user asked for.

## 6. Components

Each unit has a single purpose, a file-based interface, and explicit deps.

### 6.1 `audit_bot.py` (new) — orchestration helper, plain Python
- **Purpose:** all I/O and Zotero actions; **no LLM judgment**.
- **Subcommands:**
  - `--prepare --window-days N` → reads gh-pages data, builds FP/FN candidate
    lists, resolves Zotero keys, writes `batches/*.json` + `manifest.json`.
  - `--collect` → reads `verdicts/screen_*.json`, selects items needing
    adjudication, writes `batches/adj_*.json`.
  - `--apply [--dry-run]` → reads all verdicts, applies the §5 gates, trashes /
    rescues, writes `data/audit_log.json`.
- **Depends on:** `genebot.zotero_client.ZoteroGroupClient`,
  `run.process_rescue_queue` (imported), `bio_toolkit.clients.openalex`,
  `bio_toolkit.config`.
- **Candidate shapes (written to batch files):**
  - FP item: `{key, pmid, doi, title, abstract?, gene_or_topic, category, aliases[], disease_terms[]}`
  - FN item: `{pmid, doi, title, abstract, gene_or_topic, category, reason, effective_score, threshold}`

### 6.2 `.claude/agents/library-screener.md` (new) — **model: haiku**
- **Purpose:** cheap first pass. Judges *"is this paper genuinely about
  `gene_or_topic`?"* using the supplied aliases / disease terms (FP) or the
  rejection context (FN) — **not** general ophthalmology relevance.
- **Interface:** reads one batch file path (given in its prompt), writes
  `verdicts/screen_<batch>.json` — a list of `{id, verdict, confidence, reason}`.
- **Tools:** Read, Write only (no network needed).

### 6.3 `.claude/agents/relevance-adjudicator.md` (new) — **model: sonnet**
- **Purpose:** careful second pass on the flagged subset; the independent
  off-topic vote for trash, and the relevance decision for rescue.
- **Interface:** reads an adjudication batch path, writes `verdicts/adj_<batch>.json`.
- **Tools:** Read, Write, plus PubMed / bioRxiv connectors (optional context).

### 6.4 `ZoteroGroupClient.trash_items(keys, *, apply=False)` (new method, refactor)
- **Purpose:** factor the proven `delete_item` loop out of
  `audit_data/trash_items.py` so the CLI **and** the audit bot share one
  recoverable-trash implementation.
- **Fail-safe default:** `apply=False` is a dry-run (lists, touches nothing),
  matching the existing CLI; callers pass `apply=True` to execute. `audit_bot.py
  --apply` passes `apply=(not dry_run)`.
- `audit_data/trash_items.py` becomes a thin CLI wrapper calling this method
  (behaviour unchanged: dry-run default, `--apply` to execute).

### 6.5 Reused unchanged
- `run.process_rescue_queue(...)` — the entire FN action path. `audit_bot.py`
  builds `rescue_entries` (`{pmid, doi, subcollection, category, title}`) from
  confirmed FNs and calls it with a live `ZoteroGroupClient` + `OpenAlexClient`.
- `ZoteroGroupClient.get_existing_items()` / `get_all_items_full()` — PMID/DOI →
  key resolution and "still in active library?" checks.

### 6.6 `data/audit_log.json` (new, cumulative on gh-pages)
- Append-only record: per action `{ts, direction, pmid, key, gene_or_topic,
  screener_verdict, adjudicator_verdict, action, models}`. The reversal trail.

## 7. The routine prompt (config artifact)

Stored in the routine config (not the repo). Drives the orchestrator:

> You are auditing the Zotero group library for misfiled and wrongly-dismissed
> papers. Work strictly through the steps; do not judge relevance yourself —
> delegate every judgment to subagents.
> 1. Run `python audit_bot.py --prepare --window-days 8`. Read `manifest.json`.
> 2. For each `fp_*`/`fn_*` batch in the manifest, dispatch a **library-screener**
>    subagent, passing the batch file path. Run them in parallel.
> 3. Run `python audit_bot.py --collect`. For each `adj_*` batch, dispatch a
>    **relevance-adjudicator** subagent. Run them in parallel.
> 4. Run `python audit_bot.py --apply`. (First-ever run: add `--dry-run`.)
> 5. Commit `data/audit_log.json` to the `gh-pages` branch and print a summary:
>    counts trashed / rescued / kept, with one line each.
> If any step's script exits non-zero, stop and report; do not act on partial data.

## 8. Data flow & idempotency

- **Recent window:** `--window-days` (default 8, overlapping the weekly cadence).
  After `git fetch origin gh-pages`, read `near_misses.json` +
  `recent_additions.json` via `git show origin/gh-pages:site/data/<file>` — no
  extra network host beyond the clone.
- **Idempotency is inherent** (no watermark needed):
  - Trashed FP items leave the **active** library, so `--prepare`'s
    "still in active library" check excludes them next run.
  - Rescued FN items enter the library, so `process_rescue_queue()`'s existing
    `existing_pmids` / `existing_dois` skip re-adds.
  - Re-judging an item the routine chose to **keep** (within the overlap window)
    is harmless — same verdict — and bounded by the small window.

## 9. Error handling

- **Zotero baseline fetch fails:** `get_existing_items()` already raises
  `RuntimeError` on failure; `audit_bot.py --prepare` lets it propagate and
  exits non-zero → routine stops (mirrors `run.py main()` abort-on-outage).
- **OpenAlex lookup fails for a rescue:** `process_rescue_queue()` already
  returns it in `failed_entries`; we log and move on (no retry file needed —
  next run's window re-includes it).
- **Subagent returns malformed JSON / no file:** `--collect` / `--apply` treat a
  missing or unparseable verdict as `uncertain` for FN (→ no rescue) and as
  **not-concurring** for FP (→ **no trash**). Fail safe = no destructive action.
- **Partial subagent failure:** items lacking a verdict are simply not acted on;
  logged as `skipped` in the audit log.

## 10. Scheduling & environment (routine config)

- **Schedule:** weekly. Create via `/schedule weekly library audit`, then
  `/schedule update` to set cron `0 20 * * 0` (Sun 20:00 local; min interval 1h).
- **Repo:** `zotero-bot`. Default-branch clone. Pushing `audit_log.json` to
  `gh-pages` requires **Allow unrestricted branch pushes** for that repo
  (otherwise the routine can only push `claude/`-prefixed branches — fallback:
  commit the log to a `claude/audit-<date>` branch instead).
- **Env vars:** `ZOTERO_API_KEY`, `ZOTERO_GROUP_ID`, `OPENALEX_API_KEY`.
- **Setup script (cached):** `pip install -r requirements.txt` + the pinned
  `pip install "bio_toolkit @ git+https://github.com/Hippolyte-Menou/bio_toolkit@<sha>"`
  (same line CI uses).
- **Network access:** add `api.zotero.org` and `api.openalex.org` to the
  environment's allowed domains (PubMed/bioRxiv go through the connector proxy).
- **Connectors:** keep PubMed + bioRxiv (used by the adjudicator); remove the rest.
- **Auth prerequisite:** `/schedule` needs a claude.ai login (not API-key /
  Bedrock / Vertex) and CLI ≥ v2.1.81.

## 11. Testing

Unit tests (stdlib `unittest`, matching the existing `tests/` style) for the
**pure logic** in `audit_bot.py` — no network:
- FP candidate filtering: window cutoff, "still in active library" exclusion,
  PMID/DOI→key resolution (incl. DOI-only items).
- FN candidate filtering: window cutoff, already-in-library exclusion,
  `rescue_entries` construction from a near-miss record.
- The **concurrence gate**: truth table over (screener, adjudicator) verdicts →
  {trash, rescue, keep}; verify malformed/missing verdict ⇒ no destructive action.
- `trash_items()` refactor: CLI dry-run output unchanged (golden test).

Manual shakedown: first real run with `--apply --dry-run` (logs intended
actions, touches nothing), inspect `audit_log.json`, then enable live.

## 12. Reuse map (explicit)

| Need | Reused code |
|---|---|
| Trash an item (recoverable) | `delete_item` loop from `audit_data/trash_items.py` → `ZoteroGroupClient.trash_items()` |
| Rescue / re-add a paper | `run.process_rescue_queue()` (unchanged) |
| PMID/DOI ↔ key, dedup baseline | `ZoteroGroupClient.get_existing_items()` / `get_all_items_full()` |
| OpenAlex record build | `OpenAlexClient.work_to_record()` / `fetch_works_by_pmids/dois()` |
| Recent / near-miss data | existing `recent_additions.json` / `near_misses.json` on gh-pages |
| Credentials | `bio_toolkit.config` (`ZOTERO_GROUP_ID`, `zotero_api_key()`) |

## 13. Open questions / future iterations

- Surface audit actions in the dashboard (a "Library Audit" tab) for after-the-fact
  visibility — deferred; the user chose direct correction only.
- Rotating back-catalogue sweep to catch pre-window mistakes — deferred.
- Tune `--window-days` and cadence once real action volumes are known.
- If gh-pages push is undesirable, fall back to `claude/`-branch audit logs.
