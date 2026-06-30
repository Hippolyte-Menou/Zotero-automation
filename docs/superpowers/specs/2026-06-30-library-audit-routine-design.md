# Library Audit Routine — design

- **Date:** 2026-06-30
- **Status:** Approved (design), pending spec review → implementation plan
- **Project:** zotero-bot
- **Author:** brainstormed with Claude

## 1. Problem

The two pipelines (`run.py --genes` / `--topics`) optimise recall, so two error
classes accumulate in the shared Zotero group library:

- **False positives** — off-topic papers filed under a gene/topic they do not
  belong to. Visible right now in `site/data/recent_additions.json`, e.g.
  *"Neurosyphilis presenting as ocular motor nerve palsy"* under **ACO2**,
  *"Predictors of adult ICU mortality in Ethiopia"* under **FBN1**,
  *"hippocampal engagement … parietal TMS"* under **PRPH2** — all judgeable as
  wrong from the **title alone**. These are not just recent: 12 pipeline runs
  have salted them throughout the **existing library**.
- **False negatives** — genuinely relevant papers rejected and logged as
  near-misses (`site/data/near_misses.json`), mostly by `mention_filter` or
  `score_below_threshold`.

We want a **Claude Code Routine** (cloud, scheduled) that self-corrects both
classes by acting **directly on the Zotero API**, using **subagents pinned to
Haiku and Sonnet** to keep token cost low. **First release runs daily and must
work through the existing backlog**, not only new arrivals.

## 2. Goals / non-goals

**Goals**
- Autonomously **trash** off-topic items and **rescue** wrongly-dismissed items
  via the Zotero API.
- **Clear the existing backlog** — the full active library and the near-miss
  pool — not just new activity, **paced across daily runs**.
- Concentrate token spend on **Haiku (bulk) + Sonnet (adjudication)** subagents;
  keep the orchestrator lean.
- **Reuse existing code** for both actions (`audit_data/trash_items.py`'s
  `delete_item` trash; `run.py:process_rescue_queue()` for rescue).

**Non-goals (this iteration)**
- No human triage queue / dashboard tab. (Possible later iteration — §13.)
- **No per-run action caps** (explicit user decision). The `--max-items` knob
  paces the *sweep*; it never limits how many items are trashed/rescued within a
  slice.
- Not a one-shot full sweep — the backlog is worked through over successive runs
  via the audited ledger (§8).

## 3. Fixed decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Delivery vehicle | Cloud **Routine** (only path giving model-pinned subagents on a schedule) |
| Action policy | **Auto-act** on both directions, directly via Zotero API |
| Scope | **New activity + a paced slice of the existing backlog** (full library + near-misses), unified via an audited-PMID **ledger** |
| Sweep pacing | **`--max-items` per direction per run** — a throughput knob, **not** an action cap |
| Output surface | **None** — corrects the library directly (session transcript + audit log are the record) |
| Verification topology | **A — asymmetric two-tier** (§5) |
| Trash / rescue code | **Reuse existing** (`delete_item`; `process_rescue_queue`) |
| Cadence | **Daily (first release)**, cron `0 20 * * *` local; adjustable via `/schedule update` |
| Orchestrator model | **Sonnet** |

## 4. Architecture (one scheduled cloud run)

```
Routine session  (orchestrator model: Sonnet)
  setup: clone zotero-bot · pip install -r requirements.txt + bio_toolkit · creds from env
  │
  1. PREPARE   python audit_bot.py --prepare --max-items 400
  │     • git-read near_misses.json + audit_state.json (the ledger) from gh-pages
  │     • FP pool = ZoteroGroupClient.get_all_items_full(), minus ledger-audited ids,
  │              ordered newest-first; take top --max-items
  │     • FN pool = near_misses (reason in {score_below_threshold, mention_filter},
  │              not in library, not ledger-audited), ordered closest-to-threshold;
  │              take top --max-items
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
  │     • record EVERY judged id in the ledger (any outcome) so the sweep advances
  │     • append data/audit_log.json
  │
  5. RECORD    git-commit audit_state.json + audit_log.json to gh-pages; print summary
```

The orchestrator never holds article text: `--prepare` writes batches to disk,
subagents read their own batch by path and write verdict files, and `--apply`
reads verdict files. Token spend lands on the pinned subagents.

## 5. Verification topology (A — asymmetric two-tier)

Risk is asymmetric: **rescue is reversible** (a wrong rescue is just another FP,
caught by the FP sweep), **trash is destructive** on a shared library. Gates
differ:

- **Rescue (FN):** Haiku pre-filters (`correctly_rejected` dropped cheaply);
  Sonnet adjudicates the rest; rescue on a **single** Sonnet `relevant` verdict.
- **Trash (FP):** Haiku screens; only `off_topic`/`uncertain` go to Sonnet;
  **trash only when Haiku said `off_topic` AND Sonnet says `off_topic`**
  (two independent models concur). A screener `uncertain` that Sonnet later
  calls `off_topic` is **kept** and logged (one vote ≠ concurrence). Deliberately
  conservative; relaxing to "Sonnet decides" is a one-line change.

Everything stays within the Haiku/Sonnet tier the user asked for.

## 6. Components

Each unit has a single purpose, a file-based interface, and explicit deps.

### 6.1 `audit_bot.py` (new) — orchestration helper, plain Python
- **Purpose:** all I/O, sweep bookkeeping, and Zotero actions; **no LLM judgment**.
- **Subcommands:**
  - `--prepare --max-items N` → reads gh-pages near_misses + ledger, builds the
    FP pool (full library minus audited) and FN pool (genuine near-misses minus
    audited/in-library), applies ordering, takes the top N per direction, writes
    `batches/*.json` + `manifest.json`.
  - `--collect` → reads `verdicts/screen_*.json`, selects items needing
    adjudication, writes `batches/adj_*.json`.
  - `--apply [--dry-run]` → reads all verdicts, applies §5 gates, trashes /
    rescues, **records every judged id in the ledger**, writes `data/audit_log.json`.
- **Depends on:** `genebot.zotero_client.ZoteroGroupClient`,
  `run.process_rescue_queue` (imported), `bio_toolkit.clients.openalex`,
  `bio_toolkit.config`.
- **Candidate shapes (written to batch files):**
  - FP item: `{id, key, pmid, doi, title, abstract?, gene_or_topic, category, aliases[], disease_terms[]}`
  - FN item: `{id, pmid, doi, title, abstract, gene_or_topic, category, reason, effective_score, threshold}`
  - `id` = PMID, else lowercased DOI, else Zotero key (stable ledger key).

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
- **Fail-safe default:** `apply=False` is a dry-run; callers pass `apply=True` to
  execute. `audit_bot.py --apply` passes `apply=(not dry_run)`.
- `audit_data/trash_items.py` becomes a thin CLI wrapper calling this method.

### 6.5 Reused unchanged
- `run.process_rescue_queue(...)` — the entire FN action path. `audit_bot.py`
  builds `rescue_entries` (`{pmid, doi, subcollection, category, title}`) from
  confirmed FNs and calls it with a live `ZoteroGroupClient` + `OpenAlexClient`.
- `ZoteroGroupClient.get_all_items_full()` / `get_existing_items()` — the FP pool
  (key + title + abstract + gene/topic per item) and the in-library check.

### 6.6 `data/audit_log.json` (new, cumulative on gh-pages)
- Append-only record: per action `{ts, direction, id, key, gene_or_topic,
  screener_verdict, adjudicator_verdict, action, models}`. The reversal trail.

### 6.7 `data/audit_state.json` (new, the sweep ledger, on gh-pages)
- `{audited_ids: [...], updated_at}`. Every id the routine *judges* (trashed,
  rescued, **or kept**) is added. `--prepare` excludes audited ids from both
  pools, so the sweep advances and never re-screens the same item. New library
  items / new near-misses are un-audited → picked up automatically. This is the
  one piece of state that makes "new + backlog" a single mechanism.

## 7. The routine prompt (config artifact)

Stored in the routine config (not the repo). Drives the orchestrator:

> You are auditing the Zotero group library for misfiled and wrongly-dismissed
> papers, working through the backlog. Do not judge relevance yourself — delegate
> every judgment to subagents.
> 1. Run `python audit_bot.py --prepare --max-items 400`. Read `manifest.json`.
> 2. For each `fp_*`/`fn_*` batch, dispatch a **library-screener** subagent with
>    the batch file path. Run them in parallel.
> 3. Run `python audit_bot.py --collect`. For each `adj_*` batch, dispatch a
>    **relevance-adjudicator** subagent. Run them in parallel.
> 4. Run `python audit_bot.py --apply`. (First-ever run: add `--dry-run`.)
> 5. Commit `data/audit_state.json` and `data/audit_log.json` to the `gh-pages`
>    branch and print a summary: counts trashed / rescued / kept, one line each.
> If any step's script exits non-zero, stop and report; do not act on partial data.

## 8. Sweep mechanics & idempotency

- **The ledger drives the sweep.** `audit_state.json.audited_ids` is the set of
  already-judged ids. `--prepare` removes them from both pools; `--apply` adds
  every newly-judged id. Over successive daily runs the backlog is consumed
  `--max-items` at a time; once everything is audited, only genuinely new items
  remain each day.
- **Actions are also self-idempotent** (belt and braces): trashed FP items leave
  the active library (excluded by `get_all_items_full`); rescued FN items enter
  it (`process_rescue_queue` skips already-present pmids/dois). So even a lost
  ledger only causes re-screening, never double-acting.
- **Ordering** (value first): FP newest-first (`dateAdded` desc); FN by
  `effective_score / threshold` desc (closest to passing first). `text_exclusion`
  and `mesh_exclusion` near-misses are **excluded** — rejected for cause, not
  wrongly dismissed.
- **No recency window.** Replaced by ledger + `--max-items`; new items are simply
  un-audited and float to the top by ordering.
- **Read** near_misses + ledger via `git fetch origin gh-pages` then
  `git show origin/gh-pages:site/data/<file>` — no extra network host.

## 9. Error handling

- **Zotero baseline fetch fails:** `get_existing_items()` already raises
  `RuntimeError`; `--prepare` propagates and exits non-zero → routine stops
  (mirrors `run.py main()` abort-on-outage). Ledger untouched → no lost progress.
- **OpenAlex lookup fails for a rescue:** `process_rescue_queue()` returns it in
  `failed_entries`; logged. The id is **not** ledgered, so it retries next run.
- **Subagent returns malformed JSON / no file:** missing verdict ⇒ `uncertain`
  for FN (no rescue) and **not-concurring** for FP (**no trash**). The id is also
  not ledgered, so it is re-screened next run. Fail safe = no destructive action.
- **Partial subagent failure:** un-judged items are simply not acted on and not
  ledgered; logged as `skipped`.

## 10. Scheduling & environment (routine config)

- **Schedule:** daily. Create via `/schedule daily library audit`, then
  `/schedule update` to set cron `0 20 * * *` (20:00 local; min interval 1h).
- **Repo:** `zotero-bot`, default-branch clone. Persisting the ledger +
  audit log to `gh-pages` requires **Allow unrestricted branch pushes** for the
  repo. This is **required**, not optional: without persisted `audit_state.json`
  the sweep cannot advance (it would re-screen from the top every run). The
  existing CI already pushes data to gh-pages with `keep_files: true`, so the
  pattern is established.
- **Env vars:** `ZOTERO_API_KEY`, `ZOTERO_GROUP_ID`, `OPENALEX_API_KEY`.
- **Setup script (cached):** `pip install -r requirements.txt` + the pinned
  `pip install "bio_toolkit @ git+https://github.com/Hippolyte-Menou/bio_toolkit@<sha>"`.
- **Network access:** add `api.zotero.org` and `api.openalex.org` to allowed
  domains (PubMed/bioRxiv go through the connector proxy).
- **Connectors:** keep PubMed + bioRxiv (used by the adjudicator); remove the rest.
- **Daily run cap:** one routine = 1 run/day; well within Pro 5 / Max 15.
- **Auth prerequisite:** `/schedule` needs a claude.ai login (not API-key /
  Bedrock / Vertex) and CLI ≥ v2.1.81.

## 11. Testing

Unit tests (stdlib `unittest`, matching `tests/` style) for the **pure logic** in
`audit_bot.py` — no network:
- **Ledger exclusion:** audited ids are dropped from both pools; newly-judged ids
  are added on `--apply`; a kept item is not re-emitted next `--prepare`.
- **FP pool:** trashed/absent items excluded; `--max-items` truncation; newest-
  first ordering; PMID/DOI/key `id` derivation (incl. DOI-only items).
- **FN pool:** reason filter (only `score_below_threshold` + `mention_filter`);
  already-in-library exclusion; closest-to-threshold ordering; `rescue_entries`
  construction from a near-miss record.
- **Concurrence gate:** truth table over (screener, adjudicator) → {trash,
  rescue, keep}; malformed/missing verdict ⇒ no destructive action.
- **`trash_items()` refactor:** CLI dry-run output unchanged (golden test).

Manual shakedown: first real run with `--apply --dry-run` (logs intended
actions + would-be ledger, touches nothing), inspect, then enable live.

## 12. Reuse map (explicit)

| Need | Reused code |
|---|---|
| Trash an item (recoverable) | `delete_item` loop from `audit_data/trash_items.py` → `ZoteroGroupClient.trash_items()` |
| Rescue / re-add a paper | `run.process_rescue_queue()` (unchanged) |
| FP pool + in-library check | `ZoteroGroupClient.get_all_items_full()` / `get_existing_items()` |
| OpenAlex record build | `OpenAlexClient.work_to_record()` / `fetch_works_by_pmids/dois()` |
| Near-miss backlog | existing `near_misses.json` on gh-pages |
| Credentials | `bio_toolkit.config` (`ZOTERO_GROUP_ID`, `zotero_api_key()`) |

## 13. Open questions / future iterations

- **Tag-based ledger:** mark kept items with a Zotero `bot:audited` tag instead of
  the file ledger — survives without gh-pages and is user-visible, at the cost of
  a tag write per item. Deferred; the file ledger is simpler for v1.
- Surface audit actions in the dashboard (a "Library Audit" tab) — deferred.
- Tune `--max-items` and cadence once real action/throughput volumes are known;
  step down from daily to weekly once the backlog is cleared.
- Re-sweep policy: when/whether to clear the ledger to re-audit the whole library
  (e.g., after exclusion-list changes).
