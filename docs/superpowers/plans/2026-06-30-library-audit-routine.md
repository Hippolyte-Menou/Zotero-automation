# Library Audit Routine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily Claude Code Routine that autonomously trashes off-topic library items and rescues wrongly-dismissed near-misses via the Zotero API, using Haiku (bulk) + Sonnet (adjudication) subagents, working through the existing backlog paced by an audited-id ledger.

**Architecture:** A plain-Python orchestration helper (`audit_bot.py`) does all I/O and Zotero actions (`--prepare` builds candidate batches, `--collect` builds adjudication batches, `--apply` acts). The relevance judgment is delegated to two model-pinned subagents (`.claude/agents/*.md`). Trashing reuses the existing `delete_item` loop (factored into `ZoteroGroupClient.trash_items()`); rescuing reuses `run.process_rescue_queue()` unchanged. A `data/audit_state.json` ledger of judged ids drives the backlog sweep.

**Tech Stack:** Python 3.10+, stdlib `unittest`, `pyzotero` (via `genebot.zotero_client.ZoteroGroupClient`), `bio_toolkit` (config + OpenAlex client), Claude Code Routines + subagents.

**Reference spec:** `docs/superpowers/specs/2026-06-30-library-audit-routine-design.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `audit_bot.py` | Orchestration helper: pure selection/decision logic + thin network/CLI glue. No LLM judgment. | Create |
| `tests/test_audit_bot.py` | Unit tests for the pure logic in `audit_bot.py`. | Create |
| `genebot/zotero_client.py` | Add `trash_items()` method (factored recoverable-trash loop) + `date_added` field in `get_all_items_full()`. | Modify |
| `audit_data/trash_items.py` | Refactor CLI to call `ZoteroGroupClient.trash_items()`. | Modify |
| `.claude/agents/library-screener.md` | Haiku first-pass screener subagent. | Create |
| `.claude/agents/relevance-adjudicator.md` | Sonnet adjudicator subagent. | Create |
| `routines/library-audit.prompt.md` | The routine's orchestrator prompt + setup notes (reference copy of the cloud-routine config). | Create |
| `.gitignore` | Ignore generated `data/audit_*.json`, `audit_work/`. | Modify |
| `CLAUDE.md` | Document the new module + routine in project structure. | Modify |

**Module shape of `audit_bot.py`** (pure functions first, thin `main()` glue last):
`stable_id`, `load_ledger`, `save_ledger`, `select_fp_candidates`, `select_fn_candidates`,
`compute_apply`, `build_rescue_entries`, `chunk`, `write_batches`, `load_batch_items`,
`load_verdicts`, `select_for_adjudication`, then `cmd_prepare`, `cmd_collect`, `cmd_apply`, `main`.

---

## Task 1: Scaffolding + gitignore

**Files:**
- Create: `audit_bot.py`
- Create: `tests/test_audit_bot.py`
- Modify: `.gitignore`

- [ ] **Step 1: Create `audit_bot.py` with module docstring + imports**

```python
"""Library audit bot: trash off-topic items and rescue wrongly-dismissed near-misses.

Plain-Python orchestration helper for the daily Library Audit Routine. Does all
I/O, sweep bookkeeping, and Zotero actions; the relevance judgment is delegated
to the library-screener (Haiku) and relevance-adjudicator (Sonnet) subagents.

Subcommands:
    python audit_bot.py --prepare --max-items 400   # build candidate batches
    python audit_bot.py --collect                   # build adjudication batches
    python audit_bot.py --apply [--dry-run]         # act + update ledger/log

Credentials come from bio_toolkit.config (ZOTERO_API_KEY env or toolkit secret);
the group id lives in bio_toolkit.config.
"""

import argparse
import datetime
import json
import logging
import os

logger = logging.getLogger("audit_bot")

REASON_RESCUE_ELIGIBLE = {"score_below_threshold", "mention_filter"}
```

- [ ] **Step 2: Create `tests/test_audit_bot.py` with a smoke test**

```python
import unittest

import audit_bot


class TestModuleImports(unittest.TestCase):
    def test_reason_set(self):
        self.assertEqual(
            audit_bot.REASON_RESCUE_ELIGIBLE,
            {"score_below_threshold", "mention_filter"},
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the smoke test**

Run: `python -m unittest tests.test_audit_bot -v`
Expected: PASS (1 test).

- [ ] **Step 4: Add generated-file ignores to `.gitignore`**

Append:
```
# Library audit bot (generated state + scratch)
data/audit_state.json
data/audit_log.json
audit_work/
```

- [ ] **Step 5: Commit**

```bash
git add audit_bot.py tests/test_audit_bot.py .gitignore
git commit -m "feat(audit): scaffold audit_bot module + test harness"
```

---

## Task 2: `stable_id()` — the ledger key

**Files:**
- Modify: `audit_bot.py`
- Test: `tests/test_audit_bot.py`

- [ ] **Step 1: Write the failing test**

```python
class TestStableId(unittest.TestCase):
    def test_prefers_pmid(self):
        rec = {"pmid": "123", "doi": "10.1/AbC", "zotero_key": "K"}
        self.assertEqual(audit_bot.stable_id(rec), "pmid:123")

    def test_falls_back_to_lowercased_doi(self):
        rec = {"pmid": "", "doi": "10.1/AbC", "zotero_key": "K"}
        self.assertEqual(audit_bot.stable_id(rec), "doi:10.1/abc")

    def test_falls_back_to_key(self):
        rec = {"pmid": "", "doi": "", "key": "ZK9"}
        self.assertEqual(audit_bot.stable_id(rec), "key:ZK9")

    def test_empty_when_no_identifiers(self):
        self.assertEqual(audit_bot.stable_id({}), "")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_audit_bot.TestStableId -v`
Expected: FAIL with `AttributeError: module 'audit_bot' has no attribute 'stable_id'`.

- [ ] **Step 3: Implement `stable_id`**

```python
def stable_id(rec: dict) -> str:
    """Stable ledger key for a record: pmid, else lowercased doi, else zotero key."""
    pmid = (rec.get("pmid") or "").strip()
    if pmid:
        return f"pmid:{pmid}"
    doi = (rec.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    key = (rec.get("zotero_key") or rec.get("key") or "").strip()
    return f"key:{key}" if key else ""
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_audit_bot.TestStableId -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add audit_bot.py tests/test_audit_bot.py
git commit -m "feat(audit): stable_id ledger key"
```

---

## Task 3: Ledger load/save

**Files:**
- Modify: `audit_bot.py`
- Test: `tests/test_audit_bot.py`

- [ ] **Step 1: Write the failing test**

```python
import os
import tempfile


class TestLedger(unittest.TestCase):
    def test_load_missing_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(audit_bot.load_ledger(os.path.join(d, "x.json")), set())

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "data", "audit_state.json")
            audit_bot.save_ledger(p, {"pmid:1", "pmid:2"}, now="2026-06-30T00:00:00Z")
            self.assertEqual(audit_bot.load_ledger(p), {"pmid:1", "pmid:2"})

    def test_save_writes_sorted_ids_and_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "audit_state.json")
            audit_bot.save_ledger(p, {"pmid:2", "pmid:1"}, now="2026-06-30T00:00:00Z")
            with open(p, encoding="utf-8") as f:
                payload = json.load(f)
            self.assertEqual(payload["audited_ids"], ["pmid:1", "pmid:2"])
            self.assertEqual(payload["updated_at"], "2026-06-30T00:00:00Z")

    def test_load_corrupt_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "audit_state.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(audit_bot.load_ledger(p), set())
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_audit_bot.TestLedger -v`
Expected: FAIL with `AttributeError: ... 'load_ledger'`.

- [ ] **Step 3: Implement `load_ledger` / `save_ledger`**

```python
def load_ledger(path: str) -> set:
    """Return the set of audited ids; empty set if missing or unreadable."""
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("audited_ids", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_ledger(path: str, audited_ids: set, *, now: str = None) -> None:
    """Write the ledger (sorted ids + updated_at). `now` injectable for tests."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "audited_ids": sorted(audited_ids),
        "updated_at": now or datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_audit_bot.TestLedger -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add audit_bot.py tests/test_audit_bot.py
git commit -m "feat(audit): audited-id ledger load/save"
```

---

## Task 4: `select_fp_candidates()` — false-positive pool

**Files:**
- Modify: `audit_bot.py`
- Test: `tests/test_audit_bot.py`

Input items are shaped like `ZoteroGroupClient.get_all_items_full()` output plus a
`date_added` field (added in Task 9): `{pmid, doi, zotero_key, title, abstract,
category, subcollection, date_added, ...}`.

- [ ] **Step 1: Write the failing test**

```python
class TestSelectFpCandidates(unittest.TestCase):
    def _lib(self):
        return [
            {"pmid": "1", "doi": "", "zotero_key": "KA", "title": "old",
             "abstract": "a", "subcollection": "ACO2", "category": "6 - Genes",
             "date_added": "2026-01-01T00:00:00Z"},
            {"pmid": "2", "doi": "", "zotero_key": "KB", "title": "new",
             "abstract": "b", "subcollection": "FBN1", "category": "6 - Genes",
             "date_added": "2026-06-01T00:00:00Z"},
        ]

    def test_excludes_audited(self):
        out = audit_bot.select_fp_candidates(self._lib(), {"pmid:2"}, 10)
        self.assertEqual([c["id"] for c in out], ["pmid:1"])

    def test_orders_newest_first(self):
        out = audit_bot.select_fp_candidates(self._lib(), set(), 10)
        self.assertEqual([c["id"] for c in out], ["pmid:2", "pmid:1"])

    def test_max_items_truncates_after_ordering(self):
        out = audit_bot.select_fp_candidates(self._lib(), set(), 1)
        self.assertEqual([c["id"] for c in out], ["pmid:2"])

    def test_candidate_shape(self):
        out = audit_bot.select_fp_candidates(self._lib(), set(), 1)
        self.assertEqual(out[0], {
            "id": "pmid:2", "key": "KB", "pmid": "2", "doi": "",
            "title": "new", "abstract": "b", "kind": "fp",
            "gene_or_topic": "FBN1", "category": "6 - Genes",
        })

    def test_skips_items_without_identifier(self):
        lib = [{"pmid": "", "doi": "", "zotero_key": "", "title": "x"}]
        self.assertEqual(audit_bot.select_fp_candidates(lib, set(), 10), [])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_audit_bot.TestSelectFpCandidates -v`
Expected: FAIL with `AttributeError: ... 'select_fp_candidates'`.

- [ ] **Step 3: Implement `select_fp_candidates`**

```python
def select_fp_candidates(library_items: list, audited: set, max_items: int) -> list:
    """Active library items not yet audited, newest-first, capped at max_items."""
    pool = []
    for it in library_items:
        sid = stable_id(it)
        if not sid or sid in audited:
            continue
        pool.append(it)
    pool.sort(key=lambda it: it.get("date_added", ""), reverse=True)
    out = []
    for it in pool[:max_items]:
        out.append({
            "id": stable_id(it),
            "key": it.get("zotero_key", ""),
            "pmid": it.get("pmid", ""),
            "doi": it.get("doi", ""),
            "title": it.get("title", ""),
            "abstract": it.get("abstract", ""),
            "gene_or_topic": it.get("subcollection", ""),
            "category": it.get("category", ""),
            "kind": "fp",
        })
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_audit_bot.TestSelectFpCandidates -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add audit_bot.py tests/test_audit_bot.py
git commit -m "feat(audit): false-positive candidate selection"
```

---

## Task 5: `select_fn_candidates()` — false-negative pool

**Files:**
- Modify: `audit_bot.py`
- Test: `tests/test_audit_bot.py`

Inputs are near-miss records (shape from `site/data/near_misses.json` articles:
`{pmid, doi, title, abstract, subcollection, category, reason, effective_score,
threshold, search_keywords, ...}`). `existing_dois` / `trashed_dois` are
lowercased sets supplied by the caller.

- [ ] **Step 1: Write the failing test**

```python
class TestSelectFnCandidates(unittest.TestCase):
    def _nm(self):
        return [
            {"pmid": "10", "doi": "", "title": "close", "abstract": "x",
             "subcollection": "CRB1", "category": "6 - Genes",
             "reason": "score_below_threshold", "effective_score": 3,
             "threshold": 4, "search_keywords": ["CRB1"]},
            {"pmid": "11", "doi": "", "title": "far", "abstract": "y",
             "subcollection": "RHO", "category": "6 - Genes",
             "reason": "mention_filter", "effective_score": 1,
             "threshold": 4, "search_keywords": ["RHO"]},
            {"pmid": "12", "doi": "", "title": "cancer", "abstract": "z",
             "subcollection": "RHO", "category": "6 - Genes",
             "reason": "text_exclusion", "effective_score": 5,
             "threshold": 4, "search_keywords": ["RHO"]},
        ]

    def test_excludes_text_and_mesh_exclusions(self):
        out = audit_bot.select_fn_candidates(self._nm(), set(), set(), set(), set(), set(), 10)
        self.assertNotIn("pmid:12", [c["id"] for c in out])

    def test_orders_closest_to_threshold_first(self):
        out = audit_bot.select_fn_candidates(self._nm(), set(), set(), set(), set(), set(), 10)
        self.assertEqual([c["id"] for c in out], ["pmid:10", "pmid:11"])

    def test_excludes_already_in_library(self):
        out = audit_bot.select_fn_candidates(self._nm(), set(), {"10"}, set(), set(), set(), 10)
        self.assertEqual([c["id"] for c in out], ["pmid:11"])

    def test_excludes_trashed(self):
        out = audit_bot.select_fn_candidates(self._nm(), set(), set(), set(), {"10"}, set(), 10)
        self.assertEqual([c["id"] for c in out], ["pmid:11"])

    def test_excludes_audited(self):
        out = audit_bot.select_fn_candidates(self._nm(), {"pmid:10"}, set(), set(), set(), set(), 10)
        self.assertEqual([c["id"] for c in out], ["pmid:11"])

    def test_candidate_shape_carries_context(self):
        out = audit_bot.select_fn_candidates(self._nm()[:1], set(), set(), set(), set(), set(), 1)
        self.assertEqual(out[0], {
            "id": "pmid:10", "pmid": "10", "doi": "", "title": "close",
            "abstract": "x", "gene_or_topic": "CRB1", "category": "6 - Genes",
            "reason": "score_below_threshold", "effective_score": 3,
            "threshold": 4, "search_keywords": ["CRB1"], "kind": "fn",
        })
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_audit_bot.TestSelectFnCandidates -v`
Expected: FAIL with `AttributeError: ... 'select_fn_candidates'`.

- [ ] **Step 3: Implement `select_fn_candidates`**

```python
def select_fn_candidates(near_misses: list, audited: set, existing_pmids: set,
                         existing_dois: set, trashed_pmids: set, trashed_dois: set,
                         max_items: int) -> list:
    """Genuine near-misses (score/mention reasons) eligible for rescue.

    Excludes text/MeSH exclusions (rejected for cause), items already in the
    library, deliberately-trashed items, and already-audited items. Ordered by
    closeness to threshold (effective_score / threshold) descending.
    """
    def ratio(nm):
        thr = nm.get("threshold") or 0
        return (nm.get("effective_score") or 0) / thr if thr else 0.0

    pool = []
    for nm in near_misses:
        if nm.get("reason") not in REASON_RESCUE_ELIGIBLE:
            continue
        pmid = (nm.get("pmid") or "").strip()
        doi = (nm.get("doi") or "").strip().lower()
        if pmid and (pmid in existing_pmids or pmid in trashed_pmids):
            continue
        if doi and (doi in existing_dois or doi in trashed_dois):
            continue
        sid = stable_id(nm)
        if not sid or sid in audited:
            continue
        pool.append(nm)
    pool.sort(key=ratio, reverse=True)
    out = []
    for nm in pool[:max_items]:
        out.append({
            "id": stable_id(nm),
            "pmid": nm.get("pmid", ""),
            "doi": nm.get("doi", ""),
            "title": nm.get("title", ""),
            "abstract": nm.get("abstract", ""),
            "gene_or_topic": nm.get("subcollection", ""),
            "category": nm.get("category", ""),
            "reason": nm.get("reason", ""),
            "effective_score": nm.get("effective_score"),
            "threshold": nm.get("threshold"),
            "search_keywords": nm.get("search_keywords", []),
            "kind": "fn",
        })
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_audit_bot.TestSelectFnCandidates -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add audit_bot.py tests/test_audit_bot.py
git commit -m "feat(audit): false-negative candidate selection"
```

---

## Task 6: `compute_apply()` — the concurrence gate

**Files:**
- Modify: `audit_bot.py`
- Test: `tests/test_audit_bot.py`

This is the safety-critical decision step. `screen_verdicts` / `adj_verdicts`
are `{id: verdict_str}`.

- [ ] **Step 1: Write the failing test**

```python
class TestComputeApply(unittest.TestCase):
    def setUp(self):
        self.fp = [{"id": "pmid:1", "key": "K1"}, {"id": "pmid:2", "key": "K2"},
                   {"id": "pmid:3", "key": "K3"}, {"id": "pmid:4", "key": "K4"}]
        self.fn = [{"id": "pmid:5", "pmid": "5"}, {"id": "pmid:6", "pmid": "6"},
                   {"id": "pmid:7", "pmid": "7"}]

    def test_trash_requires_both_off_topic(self):
        screen = {"pmid:1": "off_topic", "pmid:2": "off_topic",
                  "pmid:3": "uncertain", "pmid:4": "on_topic"}
        adj = {"pmid:1": "off_topic", "pmid:2": "on_topic", "pmid:3": "off_topic"}
        out = audit_bot.compute_apply(self.fp, [], screen, adj)
        # pmid:1 concurs -> trash; pmid:2 Sonnet rescued it -> keep;
        # pmid:3 screener only "uncertain" (one vote) -> keep; pmid:4 on_topic -> keep
        self.assertEqual(out["to_trash_keys"], ["K1"])
        self.assertEqual(out["judged_ids"], {"pmid:1", "pmid:2", "pmid:3", "pmid:4"})

    def test_rescue_on_single_sonnet_relevant(self):
        screen = {"pmid:5": "relevant", "pmid:6": "uncertain", "pmid:7": "correctly_rejected"}
        adj = {"pmid:5": "relevant", "pmid:6": "correctly_rejected"}
        out = audit_bot.compute_apply([], self.fn, screen, adj)
        self.assertEqual([c["id"] for c in out["to_rescue"]], ["pmid:5"])
        self.assertEqual(out["judged_ids"], {"pmid:5", "pmid:6", "pmid:7"})

    def test_missing_screen_verdict_is_not_judged(self):
        out = audit_bot.compute_apply(self.fp[:1], [], {}, {})
        self.assertEqual(out["to_trash_keys"], [])
        self.assertEqual(out["judged_ids"], set())

    def test_missing_adjudication_is_not_judged(self):
        # screener flagged off_topic but no adjudicator verdict -> retry next run
        out = audit_bot.compute_apply(self.fp[:1], [], {"pmid:1": "off_topic"}, {})
        self.assertEqual(out["to_trash_keys"], [])
        self.assertEqual(out["judged_ids"], set())
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_audit_bot.TestComputeApply -v`
Expected: FAIL with `AttributeError: ... 'compute_apply'`.

- [ ] **Step 3: Implement `compute_apply`**

```python
def compute_apply(fp_candidates: list, fn_candidates: list,
                  screen_verdicts: dict, adj_verdicts: dict) -> dict:
    """Apply the asymmetric two-tier gate.

    FP -> trash iff screener==off_topic AND adjudicator==off_topic.
    FN -> rescue iff adjudicator==relevant.
    Only items that reach a terminal decision are added to judged_ids; items
    missing a needed verdict are left for the next run (fail-safe = no action).
    """
    to_trash_keys, to_rescue, judged = [], [], set()

    for c in fp_candidates:
        sv = screen_verdicts.get(c["id"])
        if sv is None:
            continue
        if sv == "on_topic":
            judged.add(c["id"])
            continue
        av = adj_verdicts.get(c["id"])  # off_topic / uncertain -> needs adjudication
        if av is None:
            continue
        judged.add(c["id"])
        if sv == "off_topic" and av == "off_topic" and c.get("key"):
            to_trash_keys.append(c["key"])

    for c in fn_candidates:
        sv = screen_verdicts.get(c["id"])
        if sv is None:
            continue
        if sv == "correctly_rejected":
            judged.add(c["id"])
            continue
        av = adj_verdicts.get(c["id"])  # relevant / uncertain -> needs adjudication
        if av is None:
            continue
        judged.add(c["id"])
        if av == "relevant":
            to_rescue.append(c)

    return {"to_trash_keys": to_trash_keys, "to_rescue": to_rescue, "judged_ids": judged}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_audit_bot.TestComputeApply -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add audit_bot.py tests/test_audit_bot.py
git commit -m "feat(audit): asymmetric two-tier concurrence gate"
```

---

## Task 7: `build_rescue_entries()`

**Files:**
- Modify: `audit_bot.py`
- Test: `tests/test_audit_bot.py`

Produces the exact dict shape `run.process_rescue_queue()` consumes.

- [ ] **Step 1: Write the failing test**

```python
class TestBuildRescueEntries(unittest.TestCase):
    def test_maps_to_rescue_queue_shape(self):
        fn = [{"id": "pmid:5", "pmid": "5", "doi": "10.1/x", "title": "T",
               "gene_or_topic": "CRB1, RHO", "category": "6 - Genes",
               "abstract": "a", "reason": "mention_filter"}]
        self.assertEqual(audit_bot.build_rescue_entries(fn), [{
            "pmid": "5", "doi": "10.1/x", "subcollection": "CRB1, RHO",
            "category": "6 - Genes", "title": "T",
        }])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_audit_bot.TestBuildRescueEntries -v`
Expected: FAIL with `AttributeError: ... 'build_rescue_entries'`.

- [ ] **Step 3: Implement `build_rescue_entries`**

```python
def build_rescue_entries(fn_to_rescue: list) -> list:
    """Map confirmed FN candidates to run.process_rescue_queue() entry dicts."""
    return [{
        "pmid": c.get("pmid", ""),
        "doi": c.get("doi", ""),
        "subcollection": c.get("gene_or_topic", ""),
        "category": c.get("category", ""),
        "title": c.get("title", ""),
    } for c in fn_to_rescue]
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_audit_bot.TestBuildRescueEntries -v`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add audit_bot.py tests/test_audit_bot.py
git commit -m "feat(audit): rescue-entry construction"
```

---

## Task 8: Batch & verdict file I/O

**Files:**
- Modify: `audit_bot.py`
- Test: `tests/test_audit_bot.py`

Helpers that bridge orchestrator ↔ subagents via files in a work dir.

- [ ] **Step 1: Write the failing test**

```python
class TestBatchIO(unittest.TestCase):
    def test_chunk(self):
        self.assertEqual(audit_bot.chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_write_then_load_batches_roundtrip(self):
        fp = [{"id": "pmid:1", "key": "K1"}, {"id": "pmid:2", "key": "K2"}]
        fn = [{"id": "pmid:5"}]
        with tempfile.TemporaryDirectory() as d:
            manifest = audit_bot.write_batches(d, fp, fn, batch_size=1)
            self.assertEqual(manifest["fp_batches"], ["fp_000", "fp_001"])
            self.assertEqual(manifest["fn_batches"], ["fn_000"])
            lfp, lfn = audit_bot.load_batch_items(d)
            self.assertEqual([c["id"] for c in lfp], ["pmid:1", "pmid:2"])
            self.assertEqual([c["id"] for c in lfn], ["pmid:5"])

    def test_load_verdicts_merges_files(self):
        with tempfile.TemporaryDirectory() as d:
            vdir = os.path.join(d, "verdicts")
            os.makedirs(vdir)
            with open(os.path.join(vdir, "screen_fp_000.json"), "w", encoding="utf-8") as f:
                json.dump([{"id": "pmid:1", "verdict": "off_topic", "confidence": 0.9}], f)
            with open(os.path.join(vdir, "screen_fn_000.json"), "w", encoding="utf-8") as f:
                json.dump([{"id": "pmid:5", "verdict": "relevant"}], f)
            v = audit_bot.load_verdicts(d, "screen_")
            self.assertEqual(v, {"pmid:1": "off_topic", "pmid:5": "relevant"})

    def test_select_for_adjudication(self):
        fp = [{"id": "pmid:1"}, {"id": "pmid:2"}, {"id": "pmid:3"}]
        fn = [{"id": "pmid:5"}, {"id": "pmid:6"}]
        screen = {"pmid:1": "off_topic", "pmid:2": "on_topic", "pmid:3": "uncertain",
                  "pmid:5": "relevant", "pmid:6": "correctly_rejected"}
        out = audit_bot.select_for_adjudication(fp, fn, screen)
        self.assertEqual([c["id"] for c in out], ["pmid:1", "pmid:3", "pmid:5"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_audit_bot.TestBatchIO -v`
Expected: FAIL with `AttributeError: ... 'chunk'`.

- [ ] **Step 3: Implement the helpers**

```python
def chunk(items: list, size: int) -> list:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _write_kind(bdir: str, prefix: str, candidates: list, batch_size: int) -> list:
    names = []
    for i, ch in enumerate(chunk(candidates, batch_size)):
        name = f"{prefix}_{i:03d}"
        with open(os.path.join(bdir, name + ".json"), "w", encoding="utf-8") as f:
            json.dump({"kind": prefix, "items": ch}, f, indent=1)
        names.append(name)
    return names


def write_batches(work_dir: str, fp_candidates: list, fn_candidates: list,
                  batch_size: int = 20) -> dict:
    """Write fp_*/fn_* batch files + manifest.json; return the manifest."""
    bdir = os.path.join(work_dir, "batches")
    os.makedirs(bdir, exist_ok=True)
    manifest = {
        "fp_batches": _write_kind(bdir, "fp", fp_candidates, batch_size),
        "fn_batches": _write_kind(bdir, "fn", fn_candidates, batch_size),
    }
    with open(os.path.join(work_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    return manifest


def load_batch_items(work_dir: str) -> tuple:
    """Reconstruct (fp_candidates, fn_candidates) from the batch files."""
    bdir = os.path.join(work_dir, "batches")
    fp, fn = [], []
    for fname in sorted(os.listdir(bdir)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(bdir, fname), encoding="utf-8") as f:
            items = json.load(f).get("items", [])
        if fname.startswith("fp_"):
            fp.extend(items)
        elif fname.startswith("fn_"):
            fn.extend(items)
    return fp, fn


def load_verdicts(work_dir: str, prefix: str) -> dict:
    """Merge verdicts/{prefix}*.json into {id: verdict}; tolerate bad files."""
    vdir = os.path.join(work_dir, "verdicts")
    out = {}
    if not os.path.isdir(vdir):
        return out
    for fname in sorted(os.listdir(vdir)):
        if not (fname.startswith(prefix) and fname.endswith(".json")):
            continue
        try:
            with open(os.path.join(vdir, fname), encoding="utf-8") as f:
                for row in json.load(f):
                    if "id" in row and "verdict" in row:
                        out[row["id"]] = row["verdict"]
        except (json.JSONDecodeError, OSError, TypeError):
            continue
    return out


def select_for_adjudication(fp_candidates: list, fn_candidates: list,
                            screen_verdicts: dict) -> list:
    """Candidates whose screen verdict requires a Sonnet second pass."""
    needs = []
    for c in fp_candidates:
        if screen_verdicts.get(c["id"]) in ("off_topic", "uncertain"):
            needs.append(c)
    for c in fn_candidates:
        if screen_verdicts.get(c["id"]) in ("relevant", "uncertain"):
            needs.append(c)
    return needs
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_audit_bot.TestBatchIO -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add audit_bot.py tests/test_audit_bot.py
git commit -m "feat(audit): batch + verdict file I/O helpers"
```

---

## Task 9: `ZoteroGroupClient.trash_items()` + `date_added` field

**Files:**
- Modify: `genebot/zotero_client.py` (add method ~after `get_all_items_full`; add one field inside it)
- Modify: `audit_data/trash_items.py` (refactor CLI to call the method)
- Test: `tests/test_zotero_trash.py` (create)

- [ ] **Step 1: Write the failing test (fake pyzotero client)**

Create `tests/test_zotero_trash.py`:
```python
import unittest

from genebot.zotero_client import ZoteroGroupClient


class FakeZot:
    def __init__(self):
        self.deleted = []
        self.items_by_key = {"K1": {"data": {"key": "K1", "title": "one"}},
                             "K2": {"data": {"key": "K2", "title": "two"}}}

    def item(self, key):
        return self.items_by_key[key]

    def delete_item(self, item):
        self.deleted.append(item["data"]["key"])


class TestTrashItems(unittest.TestCase):
    def _client(self, fake):
        c = ZoteroGroupClient.__new__(ZoteroGroupClient)  # bypass __init__/network
        c.zot = fake
        return c

    def test_dry_run_does_not_delete(self):
        fake = FakeZot()
        result = self._client(fake).trash_items(["K1", "K2"], apply=False)
        self.assertEqual(fake.deleted, [])
        self.assertEqual(result["would_trash"], ["K1", "K2"])
        self.assertEqual(result["trashed"], 0)

    def test_apply_deletes_each_key(self):
        fake = FakeZot()
        result = self._client(fake).trash_items(["K1", "K2"], apply=True)
        self.assertEqual(fake.deleted, ["K1", "K2"])
        self.assertEqual(result["trashed"], 2)

    def test_apply_counts_failures_without_raising(self):
        fake = FakeZot()
        result = self._client(fake).trash_items(["K1", "MISSING"], apply=True)
        self.assertEqual(fake.deleted, ["K1"])
        self.assertEqual(result["trashed"], 1)
        self.assertEqual(result["failed"], 1)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_zotero_trash -v`
Expected: FAIL with `AttributeError: 'ZoteroGroupClient' object has no attribute 'trash_items'`.

- [ ] **Step 3: Add the method to `genebot/zotero_client.py`**

Insert after `get_all_items_full` (before `get_collection_pmids`):
```python
    def trash_items(self, keys: list, *, apply: bool = False) -> dict:
        """Move group-library items to the (recoverable) trash by key.

        Reuses pyzotero's delete_item, which moves items to the Zotero trash;
        they can be restored from the trash in the Zotero client. Dry-run by
        default (apply=False) so destructive use must be explicit.

        Returns {"would_trash": [...], "trashed": int, "failed": int}.
        """
        if not apply:
            return {"would_trash": list(keys), "trashed": 0, "failed": 0}
        trashed = failed = 0
        for key in keys:
            try:
                self.zot.delete_item(self.zot.item(key))
                trashed += 1
            except Exception as e:
                logger.warning(f"trash_items: failed for {key}: {e}")
                failed += 1
        return {"would_trash": [], "trashed": trashed, "failed": failed}
```

- [ ] **Step 4: Add `date_added` to `get_all_items_full`'s result dict**

In `get_all_items_full`, the appended dict gains one line (so the audit bot can
order newest-first):
```python
            result.append({
                "pmid": pmid,
                "doi": doi,
                "zotero_key": zot_key,
                "title": data.get("title", ""),
                "authors": authors,
                "year": year,
                "journal": data.get("publicationTitle", ""),
                "abstract": data.get("abstractNote", ""),
                "source_tags": source_tags,
                "category": category,
                "subcollection": subcollection,
                "date_added": data.get("dateAdded", ""),
            })
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m unittest tests.test_zotero_trash -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Refactor `audit_data/trash_items.py` to reuse the method**

Replace the body of `main()` after the credential block (the `from pyzotero ...`
construction and both the dry-run and apply loops) with:
```python
    from genebot.zotero_client import ZoteroGroupClient

    zot = ZoteroGroupClient(group_id, api_key)
    result = zot.trash_items(args.keys, apply=args.apply)
    if not args.apply:
        print(
            f"DRY RUN -- would trash {len(result['would_trash'])} item(s) "
            f"(re-run with --apply to execute):"
        )
        for key in result["would_trash"]:
            print(f"  would trash: {key}")
        return
    print(f"\nDone: {result['trashed']} trashed, {result['failed']} failed")
    if result["failed"]:
        sys.exit(1)
```

Note: confirm `ZoteroGroupClient.__init__` accepts `(group_id, api_key)` — if its
signature differs, construct it the way `run.py` does and adapt this call.

- [ ] **Step 7: Verify the CLI still imports and dry-runs**

Run: `python audit_data/trash_items.py FAKEKEY`
Expected: prints `DRY RUN -- would trash 1 item(s) ...` and exits 0 (no network — dry-run lists keys without fetching).

- [ ] **Step 8: Commit**

```bash
git add genebot/zotero_client.py audit_data/trash_items.py tests/test_zotero_trash.py
git commit -m "refactor(zotero): shared trash_items() + date_added field"
```

---

## Task 10: `--prepare` command wiring

**Files:**
- Modify: `audit_bot.py`
- Test: `tests/test_audit_bot.py`

Split into a pure `prepare_pools()` (tested) and a thin `cmd_prepare()` that does
the network fetches and file reads.

- [ ] **Step 1: Write the failing test for `prepare_pools`**

```python
class TestPreparePools(unittest.TestCase):
    def test_builds_both_pools_and_writes_batches(self):
        library = [{"pmid": "1", "zotero_key": "K1", "title": "t",
                    "subcollection": "ACO2", "category": "6 - Genes",
                    "date_added": "2026-06-01T00:00:00Z"}]
        near = [{"pmid": "5", "title": "n", "subcollection": "CRB1",
                 "category": "6 - Genes", "reason": "score_below_threshold",
                 "effective_score": 3, "threshold": 4, "search_keywords": []}]
        with tempfile.TemporaryDirectory() as d:
            manifest = audit_bot.prepare_pools(
                work_dir=d, library_items=library, near_misses=near,
                audited=set(), existing_pmids=set(), existing_dois=set(),
                trashed_pmids=set(), trashed_dois=set(), max_items=400, batch_size=20)
            self.assertEqual(manifest["fp_batches"], ["fp_000"])
            self.assertEqual(manifest["fn_batches"], ["fn_000"])
            fp, fn = audit_bot.load_batch_items(d)
            self.assertEqual([c["id"] for c in fp], ["pmid:1"])
            self.assertEqual([c["id"] for c in fn], ["pmid:5"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_audit_bot.TestPreparePools -v`
Expected: FAIL with `AttributeError: ... 'prepare_pools'`.

- [ ] **Step 3: Implement `prepare_pools` + `cmd_prepare`**

```python
def prepare_pools(*, work_dir, library_items, near_misses, audited,
                  existing_pmids, existing_dois, trashed_pmids, trashed_dois,
                  max_items, batch_size=20) -> dict:
    """Pure assembler: build FP/FN candidate pools and write batch files."""
    fp = select_fp_candidates(library_items, audited, max_items)
    fn = select_fn_candidates(near_misses, audited, existing_pmids, existing_dois,
                              trashed_pmids, trashed_dois, max_items)
    manifest = write_batches(work_dir, fp, fn, batch_size=batch_size)
    logger.info("prepare: %d FP candidates, %d FN candidates", len(fp), len(fn))
    return manifest


def _read_json(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def cmd_prepare(args) -> None:
    from genebot.zotero_client import ZoteroGroupClient
    from bio_toolkit.config import ZOTERO_GROUP_ID, zotero_api_key

    zot = ZoteroGroupClient(str(ZOTERO_GROUP_ID), zotero_api_key())
    library = zot.get_all_items_full()
    existing_pmids = set(zot.get_existing_items())          # raises on failure (safety)
    existing_dois = {d.lower() for d in zot.get_existing_dois()}
    trashed_pmids = zot.get_trashed_pmids()
    trashed_dois = {d.lower() for d in zot.get_trashed_dois()}

    near = _read_json(os.path.join(args.data_dir, "near_misses.json"), {})
    near_misses = near.get("articles", []) if isinstance(near, dict) else near
    audited = load_ledger(args.ledger)

    manifest = prepare_pools(
        work_dir=args.work_dir, library_items=library, near_misses=near_misses,
        audited=audited, existing_pmids=existing_pmids, existing_dois=existing_dois,
        trashed_pmids=trashed_pmids, trashed_dois=trashed_dois,
        max_items=args.max_items, batch_size=args.batch_size)
    print(f"prepared {len(manifest['fp_batches'])} FP + "
          f"{len(manifest['fn_batches'])} FN batches in {args.work_dir}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_audit_bot.TestPreparePools -v`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add audit_bot.py tests/test_audit_bot.py
git commit -m "feat(audit): --prepare pool assembly + wiring"
```

---

## Task 11: `--collect` command wiring

**Files:**
- Modify: `audit_bot.py`
- Test: `tests/test_audit_bot.py`

- [ ] **Step 1: Write the failing test**

```python
class TestCmdCollect(unittest.TestCase):
    def test_builds_adjudication_batches_from_screen_verdicts(self):
        fp = [{"id": "pmid:1", "key": "K1"}, {"id": "pmid:2", "key": "K2"}]
        fn = [{"id": "pmid:5"}]
        with tempfile.TemporaryDirectory() as d:
            audit_bot.write_batches(d, fp, fn, batch_size=20)
            vdir = os.path.join(d, "verdicts")
            os.makedirs(vdir)
            with open(os.path.join(vdir, "screen_fp_000.json"), "w", encoding="utf-8") as f:
                json.dump([{"id": "pmid:1", "verdict": "off_topic"},
                           {"id": "pmid:2", "verdict": "on_topic"}], f)
            with open(os.path.join(vdir, "screen_fn_000.json"), "w", encoding="utf-8") as f:
                json.dump([{"id": "pmid:5", "verdict": "relevant"}], f)
            manifest = audit_bot.collect_adjudication(d, batch_size=20)
            self.assertEqual(manifest["adj_batches"], ["adj_000"])
            with open(os.path.join(d, "batches", "adj_000.json"), encoding="utf-8") as f:
                ids = [c["id"] for c in json.load(f)["items"]]
            self.assertEqual(sorted(ids), ["pmid:1", "pmid:5"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_audit_bot.TestCmdCollect -v`
Expected: FAIL with `AttributeError: ... 'collect_adjudication'`.

- [ ] **Step 3: Implement `collect_adjudication` + `cmd_collect`**

```python
def collect_adjudication(work_dir: str, batch_size: int = 20) -> dict:
    """Read screen verdicts + batches, write adj_* batches for the flagged subset."""
    fp, fn = load_batch_items(work_dir)
    screen = load_verdicts(work_dir, "screen_")
    needs = select_for_adjudication(fp, fn, screen)
    bdir = os.path.join(work_dir, "batches")
    names = _write_kind(bdir, "adj", needs, batch_size)
    manifest = {"adj_batches": names}
    with open(os.path.join(work_dir, "adj_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    logger.info("collect: %d items need adjudication", len(needs))
    return manifest


def cmd_collect(args) -> None:
    manifest = collect_adjudication(args.work_dir, batch_size=args.batch_size)
    print(f"collected {len(manifest['adj_batches'])} adjudication batch(es)")
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_audit_bot.TestCmdCollect -v`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add audit_bot.py tests/test_audit_bot.py
git commit -m "feat(audit): --collect adjudication batching"
```

---

## Task 12: `--apply` command wiring (actions + ledger + log)

**Files:**
- Modify: `audit_bot.py`
- Test: `tests/test_audit_bot.py`

`apply_actions()` is testable by injecting a fake Zotero client and a stub
rescue function; the network setup stays in `cmd_apply`.

- [ ] **Step 1: Write the failing test**

```python
class FakeZotForApply:
    def __init__(self):
        self.trashed = []

    def trash_items(self, keys, *, apply):
        if apply:
            self.trashed.extend(keys)
        return {"would_trash": list(keys), "trashed": len(keys) if apply else 0,
                "failed": 0}


class TestApplyActions(unittest.TestCase):
    def test_trashes_rescues_and_ledgers(self):
        fp = [{"id": "pmid:1", "key": "K1"}, {"id": "pmid:2", "key": "K2"}]
        fn = [{"id": "pmid:5", "pmid": "5", "gene_or_topic": "CRB1",
               "category": "6 - Genes", "title": "T", "doi": ""}]
        screen = {"pmid:1": "off_topic", "pmid:2": "on_topic", "pmid:5": "relevant"}
        adj = {"pmid:1": "off_topic", "pmid:5": "relevant"}
        fake = FakeZotForApply()
        rescued = []

        def fake_rescue(entries):
            rescued.extend(entries)
            return (len(entries), [])

        with tempfile.TemporaryDirectory() as d:
            ledger_path = os.path.join(d, "audit_state.json")
            log_path = os.path.join(d, "audit_log.json")
            summary = audit_bot.apply_actions(
                fp, fn, screen, adj, zot=fake, rescue_fn=fake_rescue,
                ledger_path=ledger_path, log_path=log_path,
                apply=True, now="2026-06-30T00:00:00Z")

            self.assertEqual(fake.trashed, ["K1"])
            self.assertEqual([e["pmid"] for e in rescued], ["5"])
            self.assertEqual(summary["trashed"], 1)
            self.assertEqual(summary["rescued"], 1)
            self.assertEqual(audit_bot.load_ledger(ledger_path),
                             {"pmid:1", "pmid:2", "pmid:5"})
            log = audit_bot.load_json_list(log_path)
            self.assertEqual(len(log), 2)  # one trash + one rescue record

    def test_dry_run_acts_on_nothing_but_reports(self):
        fp = [{"id": "pmid:1", "key": "K1"}]
        screen = {"pmid:1": "off_topic"}
        adj = {"pmid:1": "off_topic"}
        fake = FakeZotForApply()
        with tempfile.TemporaryDirectory() as d:
            summary = audit_bot.apply_actions(
                fp, [], screen, adj, zot=fake, rescue_fn=lambda e: (0, []),
                ledger_path=os.path.join(d, "s.json"),
                log_path=os.path.join(d, "l.json"),
                apply=False, now="2026-06-30T00:00:00Z")
            self.assertEqual(fake.trashed, [])
            self.assertEqual(summary["would_trash"], 1)
            # dry-run still records the ledger so the sweep advances
            self.assertEqual(audit_bot.load_ledger(os.path.join(d, "s.json")), {"pmid:1"})
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_audit_bot.TestApplyActions -v`
Expected: FAIL with `AttributeError: ... 'apply_actions'`.

- [ ] **Step 3: Implement `load_json_list`, `apply_actions`, `cmd_apply`**

```python
def load_json_list(path: str) -> list:
    data = _read_json(path, [])
    return data if isinstance(data, list) else []


def apply_actions(fp_candidates, fn_candidates, screen_verdicts, adj_verdicts, *,
                  zot, rescue_fn, ledger_path, log_path, apply, now=None) -> dict:
    """Execute the gate's decisions, update the ledger, append the audit log.

    `zot.trash_items(keys, apply=...)` and `rescue_fn(entries) -> (count, failed)`
    are injected so this is testable without network. In dry-run (apply=False)
    nothing is trashed/rescued, but judged ids are still ledgered so the sweep
    advances on the next live run.
    """
    plan = compute_apply(fp_candidates, fn_candidates, screen_verdicts, adj_verdicts)
    ts = now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    trash_result = zot.trash_items(plan["to_trash_keys"], apply=apply)

    rescued = 0
    if apply and plan["to_rescue"]:
        rescued, _failed = rescue_fn(build_rescue_entries(plan["to_rescue"]))

    # Audit log: one record per acted item.
    fp_by_id = {c["id"]: c for c in fp_candidates}
    log = load_json_list(log_path)
    key_to_id = {c["key"]: c["id"] for c in fp_candidates if c.get("key")}
    for key in plan["to_trash_keys"]:
        cid = key_to_id.get(key, "")
        log.append({"ts": ts, "direction": "fp", "action": "trash", "id": cid,
                    "key": key, "gene_or_topic": fp_by_id.get(cid, {}).get("gene_or_topic", ""),
                    "screener_verdict": screen_verdicts.get(cid),
                    "adjudicator_verdict": adj_verdicts.get(cid),
                    "applied": apply, "models": {"screener": "haiku", "adjudicator": "sonnet"}})
    for c in plan["to_rescue"]:
        log.append({"ts": ts, "direction": "fn", "action": "rescue", "id": c["id"],
                    "pmid": c.get("pmid", ""), "gene_or_topic": c.get("gene_or_topic", ""),
                    "screener_verdict": screen_verdicts.get(c["id"]),
                    "adjudicator_verdict": adj_verdicts.get(c["id"]),
                    "applied": apply, "models": {"screener": "haiku", "adjudicator": "sonnet"}})
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=1)

    # Ledger: every judged id (any outcome), so the sweep never re-screens it.
    audited = load_ledger(ledger_path) | plan["judged_ids"]
    save_ledger(ledger_path, audited, now=ts)

    return {"trashed": trash_result["trashed"], "would_trash": len(plan["to_trash_keys"]),
            "rescued": rescued, "kept": len(plan["judged_ids"]) - len(plan["to_trash_keys"]) - rescued,
            "judged": len(plan["judged_ids"])}


def cmd_apply(args) -> None:
    import run
    from genebot.zotero_client import ZoteroGroupClient
    from bio_toolkit.clients.openalex import OpenAlexClient
    from bio_toolkit.config import ZOTERO_GROUP_ID, zotero_api_key

    fp, fn = load_batch_items(args.work_dir)
    screen = load_verdicts(args.work_dir, "screen_")
    adj = load_verdicts(args.work_dir, "adj_")

    zot = ZoteroGroupClient(str(ZOTERO_GROUP_ID), zotero_api_key())
    openalex = OpenAlexClient()

    # Build the dedup context process_rescue_queue needs (get_existing_items raises on failure).
    pmid_to_key = zot.get_existing_items()
    existing_pmids = set(pmid_to_key)
    existing_dois = {d.lower() for d in zot.get_existing_dois()}
    genes_parent_key = zot.get_or_create_collection(args.genes_parent)

    def rescue_fn(entries):
        return run.process_rescue_queue(
            entries, zot, openalex, existing_pmids, existing_dois, pmid_to_key,
            genes_parent_key, additions_tracker=[])

    summary = apply_actions(
        fp, fn, screen, adj, zot=zot, rescue_fn=rescue_fn,
        ledger_path=args.ledger, log_path=args.log, apply=(not args.dry_run))
    print(f"apply: trashed={summary['trashed']} would_trash={summary['would_trash']} "
          f"rescued={summary['rescued']} kept={summary['kept']} judged={summary['judged']}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_audit_bot.TestApplyActions -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Add `main()` + argparse and verify the CLI parses**

Append:
```python
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Library audit bot")
    p.add_argument("--data-dir", default="site/data")
    p.add_argument("--work-dir", default="audit_work")
    p.add_argument("--ledger", default="data/audit_state.json")
    p.add_argument("--log", default="data/audit_log.json")
    p.add_argument("--max-items", type=int, default=400)
    p.add_argument("--batch-size", type=int, default=20)
    p.add_argument("--genes-parent", default="6 - Genes")
    p.add_argument("--dry-run", action="store_true")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--collect", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if args.prepare:
        cmd_prepare(args)
    elif args.collect:
        cmd_collect(args)
    elif args.apply:
        cmd_apply(args)


if __name__ == "__main__":
    main()
```

Run: `python audit_bot.py --help`
Expected: usage text listing `--prepare/--collect/--apply` and the options; exit 0.

- [ ] **Step 6: Run the full suite**

Run: `python -m unittest discover -s tests -v`
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add audit_bot.py tests/test_audit_bot.py
git commit -m "feat(audit): --apply actions, ledger, audit log + CLI"
```

---

## Task 13: Subagent definition files

**Files:**
- Create: `.claude/agents/library-screener.md`
- Create: `.claude/agents/relevance-adjudicator.md`

- [ ] **Step 1: Create `.claude/agents/library-screener.md`**

```markdown
---
name: library-screener
description: First-pass relevance triage for the library audit. Reads one batch file of candidate papers and writes a per-item verdict. Used for both off-topic library items (FP) and dismissed near-misses (FN).
model: haiku
tools: Read, Write
---

You screen papers for the Zotero library audit. You are the cheap first pass; a
Sonnet adjudicator double-checks anything you flag, so be decisive but do not
over-trash — when genuinely unsure, say `uncertain`.

You will be given the path to ONE batch JSON file: `{"kind": "fp"|"fn", "items": [...]}`.
Read it. For every item, judge using its `title`, `abstract`, `gene_or_topic`,
and (for FN) `search_keywords`/`reason`.

- **kind == "fp"** — the paper is currently filed under the gene/topic
  `gene_or_topic`. Decide whether it is genuinely about that gene/topic (or its
  associated diseases/biology). Verdict one of:
  `on_topic` | `off_topic` | `uncertain`.
- **kind == "fn"** — the paper was rejected from `gene_or_topic`. Decide whether
  it is genuinely relevant and worth including. Verdict one of:
  `relevant` | `correctly_rejected` | `uncertain`.

Judge each item ONLY against its own `gene_or_topic`, not general ophthalmology
relevance. A paper can be solid science yet off-topic for the gene it is under.

Write your answer to `verdicts/screen_<batchname>.json` (same basename as the
input, e.g. input `batches/fp_003.json` -> `verdicts/screen_fp_003.json`) as a
JSON list, one object per item:

```json
[{"id": "pmid:123", "verdict": "off_topic", "confidence": 0.9, "reason": "case report on neurosyphilis; no ACO2 link"}]
```

Return only a one-line confirmation of how many items you wrote. Do not call any
other tools.
```

- [ ] **Step 2: Create `.claude/agents/relevance-adjudicator.md`**

```markdown
---
name: relevance-adjudicator
description: Careful second-pass adjudication for the library audit. Confirms whether a flagged library paper is truly off-topic (the independent vote required before trashing) and whether a dismissed near-miss is genuinely relevant enough to rescue. May consult PubMed/bioRxiv connectors for context.
model: sonnet
---

You are the adjudicator for the Zotero library audit. You see only the subset the
Haiku screener flagged. Your verdict is decisive: for FP items a trash happens
ONLY if you AND the screener both say off-topic, so a wrong "off_topic" here
deletes a paper; for FN items your `relevant` triggers a re-add.

You will be given the path to ONE adjudication batch JSON file with mixed
`fp`/`fn` items. Each item carries a `kind` field (`"fp"` or `"fn"`) and keeps
its original fields, including `id`, `gene_or_topic`, `title`, `abstract`, and
for FN `reason`/`search_keywords`. Read it. Branch on each item's `kind` and
reach an independent judgment. If the abstract is thin or the gene link is
ambiguous, you MAY use the PubMed or bioRxiv connector tools to check the paper's
actual subject before deciding. Prefer caution on `fp` items: if a real
connection to `gene_or_topic` is plausible, do NOT call it off-topic.

Verdicts:
- `kind == "fp"` -> `off_topic` | `on_topic`
- `kind == "fn"` -> `relevant` | `correctly_rejected`

Write `verdicts/adj_<batchname>.json` as a JSON list of
`{"id", "verdict", "confidence", "reason"}`, then return a one-line summary.
```

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/library-screener.md .claude/agents/relevance-adjudicator.md
git commit -m "feat(audit): library-screener (haiku) + relevance-adjudicator (sonnet) subagents"
```

---

## Task 14: Routine prompt + docs

**Files:**
- Create: `routines/library-audit.prompt.md`
- Modify: `CLAUDE.md` (project-structure section)

- [ ] **Step 1: Create `routines/library-audit.prompt.md`**

```markdown
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
```

- [ ] **Step 2: Update `CLAUDE.md` project structure**

Add under the file tree (after the `dedup_library.py` line):
```
├── audit_bot.py            # Library audit routine helper: trash off-topic + rescue dismissed (Haiku/Sonnet subagents)
```
And add a `.claude/agents/` note near the bottom of the structure list:
```
├── .claude/agents/         # Routine subagents: library-screener (haiku), relevance-adjudicator (sonnet)
├── routines/               # Reference copies of cloud-routine prompts (library-audit)
```

- [ ] **Step 3: Run the full suite once more**

Run: `python -m unittest discover -s tests -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add routines/library-audit.prompt.md CLAUDE.md
git commit -m "docs(audit): routine prompt + project-structure notes"
```

---

## Manual / out-of-repo steps (after the code lands)

These cannot be done from the repo — they configure the cloud routine:

1. Create the routine at `claude.ai/code/routines` using
   `routines/library-audit.prompt.md` (or `/schedule daily library audit` then
   `/schedule update` to set cron `0 20 * * *`).
2. Set env vars, network allowlist, connectors, and the setup script per that file.
3. Enable **Allow unrestricted branch pushes** for the `gh-pages` branch.
4. Click **Run now** with the prompt's first-run `--dry-run` variant; open the
   session, read `data/audit_log.json`, confirm the intended trashes/rescues look
   right, then switch step 5 back to `--apply` (live) and run again.

---

## Self-Review (completed during planning)

**Spec coverage:** §4 flow → Tasks 10/11/12 + routine prompt (Task 14). §5 gate →
Task 6. §6.1 audit_bot → Tasks 2-12. §6.2/6.3 subagents → Task 13. §6.4
trash_items → Task 9. §6.5 reuse (process_rescue_queue) → Task 12 `cmd_apply`.
§6.6 audit_log + §6.7 ledger → Tasks 3/12. §8 sweep/ordering/trash-skip → Tasks
4/5. §10 scheduling/env → Task 14 + manual steps. §11 testing → every task is TDD.
§12 reuse map → Tasks 9/12. **Deviation from spec:** FP candidates carry symbol +
title + abstract only (no genes.yml-derived `aliases[]`/`disease_terms[]`); the
screener relies on model knowledge of the gene. This is a deliberate v1
simplification (flagged to the user); genes.yml enrichment is deferred.

**Placeholder scan:** the only literal placeholder is `<sha>` in the setup-script
pip line (intentional — the user pins the toolkit SHA, per CLAUDE.md) and the
note in Task 9 Step 6 to confirm `ZoteroGroupClient.__init__`'s signature.

**Type consistency:** candidate dicts use `id`/`key`/`gene_or_topic` consistently
across Tasks 4-12; verdict strings (`on_topic`/`off_topic`/`uncertain`,
`relevant`/`correctly_rejected`) match between the agent files (Task 13) and
`compute_apply`/`select_for_adjudication` (Tasks 6/8). `trash_items(keys, *,
apply)` and `process_rescue_queue(...)` signatures match their real definitions.
```
