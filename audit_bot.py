"""Library audit bot: trash off-topic items and rescue wrongly-dismissed near-misses.

Plain-Python orchestration helper for the daily Library Audit Routine. Does all
I/O, sweep bookkeeping, and Zotero actions; the relevance judgment is delegated
to the library-screener (Haiku) and relevance-adjudicator (Sonnet) subagents.

Subcommands:
    python audit_bot.py --prepare --max-items 400   # build candidate batches
    python audit_bot.py --collect                   # build adjudication batches
    python audit_bot.py --apply [--dry-run]         # act + update ledger/log

Credentials come from bio_toolkit.config (ZOTERO_API_KEY env or toolkit secret);
the group id lives in bio_toolkit.config. All default paths resolve relative to
this file, so the tool works regardless of the caller's working directory.
"""

import argparse
import datetime
import json
import logging
import os

logger = logging.getLogger("audit_bot")

# Anchor default paths to the repo (this file's dir) so the tool is
# CWD-independent -- a cloud step that runs from the wrong directory can no
# longer scatter audit_work/ or read an empty near_misses file.
_HERE = os.path.dirname(os.path.abspath(__file__))

REASON_RESCUE_ELIGIBLE = {"score_below_threshold", "mention_filter"}


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


def select_fp_candidates(library_items: list, audited: set, max_items: int) -> list:
    """Active library items not yet audited, newest-first, capped at max_items."""
    pool = []
    for it in library_items:
        sid = stable_id(it)
        if not sid or sid in audited:
            continue
        pool.append((sid, it))
    pool.sort(key=lambda pair: pair[1].get("date_added", ""), reverse=True)
    out = []
    for sid, it in pool[:max_items]:
        out.append({
            "id": sid,
            "kind": "fp",
            "key": it.get("zotero_key", ""),
            "pmid": it.get("pmid", ""),
            "doi": it.get("doi", ""),
            "title": it.get("title", ""),
            "abstract": it.get("abstract", ""),
            "gene_or_topic": it.get("subcollection", ""),
            "category": it.get("category", ""),
        })
    return out


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
        pool.append((sid, nm))
    pool.sort(key=lambda pair: ratio(pair[1]), reverse=True)
    out = []
    for sid, nm in pool[:max_items]:
        out.append({
            "id": sid,
            "kind": "fn",
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
        })
    return out


def derive_dedup(library_items: list) -> tuple:
    """Build (existing_pmids, existing_dois, pmid_to_key) from full library items.

    Equivalent to get_existing_items()/get_existing_dois() but reuses the single
    get_all_items_full() fetch instead of traversing the whole ~20k-item library
    a second time. DOIs are lowercased to match stable_id / dedup conventions.
    """
    existing_pmids, existing_dois, pmid_to_key = set(), set(), {}
    for it in library_items:
        pmid = (it.get("pmid") or "").strip()
        doi = (it.get("doi") or "").strip().lower()
        key = (it.get("zotero_key") or "").strip()
        if pmid:
            existing_pmids.add(pmid)
            if key:
                pmid_to_key[pmid] = key
        if doi:
            existing_dois.add(doi)
    return existing_pmids, existing_dois, pmid_to_key


def save_dedup_baseline(work_dir: str, pmid_to_key: dict, existing_dois: set) -> None:
    """Cache the dedup baseline so --apply need not re-fetch the whole library."""
    os.makedirs(work_dir, exist_ok=True)
    with open(os.path.join(work_dir, "dedup_baseline.json"), "w", encoding="utf-8") as f:
        json.dump({"pmid_to_key": pmid_to_key,
                   "existing_dois": sorted(existing_dois)}, f, indent=1)


def load_dedup_baseline(work_dir: str):
    """Return (existing_pmids, existing_dois, pmid_to_key) or None if absent/bad."""
    data = _read_json(os.path.join(work_dir, "dedup_baseline.json"), None)
    if not isinstance(data, dict) or "pmid_to_key" not in data:
        return None
    pmid_to_key = data.get("pmid_to_key", {})
    existing_dois = {d.lower() for d in data.get("existing_dois", [])}
    return set(pmid_to_key), existing_dois, pmid_to_key


def chunk(items: list, size: int) -> list:
    """Split items into sublists of at most `size` elements."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [items[i:i + size] for i in range(0, len(items), size)]


def _verdict_path(work_dir: str, batch_name: str, prefix: str) -> str:
    """Absolute path a subagent must write its verdicts to for one batch.

    Screener batches (fp_/fn_) -> verdicts/screen_<name>.json; adjudication
    batches (adj_) -> verdicts/<name>.json. Absolute so the subagent writes to
    the loader's directory regardless of its own working directory.
    """
    out_name = f"{batch_name}.json" if prefix == "adj" else f"screen_{batch_name}.json"
    return os.path.abspath(os.path.join(work_dir, "verdicts", out_name))


def _write_kind(bdir: str, prefix: str, candidates: list, batch_size: int) -> list:
    work_dir = os.path.dirname(bdir)
    os.makedirs(os.path.join(work_dir, "verdicts"), exist_ok=True)
    names = []
    for i, ch in enumerate(chunk(candidates, batch_size)):
        name = f"{prefix}_{i:03d}"
        # Each batch self-describes where its verdicts go (verdict_out), so the
        # subagent does no path arithmetic -- a recurring source of misplaced
        # verdict files that silently stalled the whole sweep.
        payload = {"kind": prefix, "items": ch,
                   "verdict_out": _verdict_path(work_dir, name, prefix)}
        with open(os.path.join(bdir, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
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
        try:
            with open(os.path.join(bdir, fname), encoding="utf-8") as f:
                items = json.load(f).get("items", [])
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("load_batch_items: skipping unreadable %s: %s", fname, e)
            continue
        # adj_* batch files also live in batches/ but are intentionally skipped
        # here: they are consumed via load_verdicts(work_dir, "adj_"), not
        # reconstructed into a candidate pool.
        if fname.startswith("fp_"):
            fp.extend(items)
        elif fname.startswith("fn_"):
            fn.extend(items)
    return fp, fn


def _merge_verdict_dir(vdir: str, prefix: str, out: dict) -> None:
    """Merge one verdicts dir's {prefix}*.json files into `out` (id -> verdict)."""
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


def load_verdicts(work_dir: str, prefix: str) -> dict:
    """Merge verdicts/{prefix}*.json into {id: verdict}; tolerate bad files.

    Looks in <work_dir>/verdicts AND a repo-root ./verdicts. Batches carry an
    explicit verdict_out under <work_dir>/verdicts, but some subagents still
    write relative to their own CWD; reading both makes the sweep robust to that
    (merging is idempotent -- verdicts are keyed by id).
    """
    out = {}
    checked = set()
    for vdir in (os.path.join(work_dir, "verdicts"),
                 os.path.join(os.getcwd(), "verdicts")):
        real = os.path.realpath(vdir)
        if real in checked or not os.path.isdir(vdir):
            continue
        checked.add(real)
        _merge_verdict_dir(vdir, prefix, out)
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


def build_rescue_entries(fn_to_rescue: list) -> list:
    """Map confirmed FN candidates to run.process_rescue_queue() entry dicts."""
    return [{
        "pmid": c.get("pmid", ""),
        "doi": c.get("doi", ""),
        "subcollection": c.get("gene_or_topic", ""),
        "category": c.get("category", ""),
        "title": c.get("title", ""),
    } for c in fn_to_rescue]


def compute_apply(fp_candidates: list, fn_candidates: list,
                  screen_verdicts: dict, adj_verdicts: dict) -> dict:
    """Apply the symmetric two-tier gate (both models must concur to act).

    FP -> trash iff screener==off_topic AND adjudicator==off_topic.
    FN -> rescue iff screener==relevant AND adjudicator==relevant.
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
        # Symmetric with trash: only rescue on two-model concurrence, so a single
        # over-eager Sonnet "relevant" can't re-add a borderline paper unchecked.
        if sv == "relevant" and av == "relevant":
            to_rescue.append(c)

    return {"to_trash_keys": to_trash_keys, "to_rescue": to_rescue, "judged_ids": judged}


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
    # ONE full-library fetch serves both FP selection and the dedup baseline
    # (previously two separate ~20k-item traversals). get_all_items_full()
    # already carries pmid/doi/zotero_key and skips only items lacking both, so
    # the dedup sets it yields are identical to get_existing_items().
    library = zot.get_all_items_full()
    if not library:
        raise RuntimeError(
            "get_all_items_full() returned no items -- refusing to build audit "
            "pools on an empty dedup baseline (would risk rescuing duplicates). "
            "Treat as a transient Zotero failure and retry next run.")
    existing_pmids, existing_dois, pmid_to_key = derive_dedup(library)
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
    # Cache the dedup baseline so --apply can skip a third full-library fetch.
    save_dedup_baseline(args.work_dir, pmid_to_key, existing_dois)
    print(f"prepared {len(manifest['fp_batches'])} FP + "
          f"{len(manifest['fn_batches'])} FN batches in {os.path.abspath(args.work_dir)}")


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
    manifest = collect_adjudication(args.work_dir, batch_size=args.adj_batch_size)
    print(f"collected {len(manifest['adj_batches'])} adjudication batch(es) in "
          f"{os.path.abspath(args.work_dir)}")


def load_json_list(path: str) -> list:
    data = _read_json(path, [])
    return data if isinstance(data, list) else []


def update_feedback(feedback_path: str, plan: dict, fp_candidates: list, *,
                    now: str) -> None:
    """Cumulative per-gene tally of confirmed trashes/rescues.

    The upstream signal the audit exposes: a gene with chronic trashes is an
    alias collision to add to genes.yml `blocked_aliases`; chronic rescues mean
    the citation threshold is too strict for that gene. Advisory only -- nothing
    in the bot consumes this; it is for the (vault-side) generator to act on.
    """
    fb = _read_json(feedback_path, {})
    if not isinstance(fb, dict):
        fb = {}
    genes = fb.setdefault("genes", {})

    def bump(gene_field, action):
        for g in (gene_field or "").split(", "):
            g = g.strip()
            if not g:
                continue
            genes.setdefault(g, {"trashed": 0, "rescued": 0})[action] += 1

    key_to_gene = {c["key"]: c.get("gene_or_topic", "")
                   for c in fp_candidates if c.get("key")}
    for key in plan["to_trash_keys"]:
        bump(key_to_gene.get(key, ""), "trashed")
    for c in plan["to_rescue"]:
        bump(c.get("gene_or_topic", ""), "rescued")

    fb["updated_at"] = now
    os.makedirs(os.path.dirname(feedback_path) or ".", exist_ok=True)
    with open(feedback_path, "w", encoding="utf-8") as f:
        json.dump(fb, f, indent=1)


def apply_actions(fp_candidates, fn_candidates, screen_verdicts, adj_verdicts, *,
                  zot, rescue_fn, ledger_path, log_path, apply, now=None,
                  feedback_path=None) -> dict:
    """Execute the gate's decisions, update the ledger, append the audit log.

    `zot.trash_items(keys, apply=...)` and `rescue_fn(entries) -> (count, failed)`
    are injected so this is testable without network. In dry-run (apply=False)
    nothing is trashed/rescued and the ledger/feedback are left untouched, so a
    later live run still acts on the same items (the audit log is still written,
    with "applied": False, so a dry-run remains inspectable). In a live run,
    judged ids are ledgered EXCEPT ids whose rescue transiently failed, which
    stay out of the ledger so they are retried on the next run.
    """
    plan = compute_apply(fp_candidates, fn_candidates, screen_verdicts, adj_verdicts)
    ts = now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    trash_result = zot.trash_items(plan["to_trash_keys"], apply=apply)

    rescued = 0
    failed_rescue_ids = set()
    if apply and plan["to_rescue"]:
        rescued, failed_entries = rescue_fn(build_rescue_entries(plan["to_rescue"]))
        # Map failed rescue entries back to their candidate ids (on pmid +
        # lowercased doi) so transiently-failed rescues are NOT ledgered and get
        # retried on the next run.
        failed_keys = {((e.get("pmid") or "").strip(),
                        (e.get("doi") or "").strip().lower()) for e in failed_entries}
        for c in plan["to_rescue"]:
            ckey = ((c.get("pmid") or "").strip(), (c.get("doi") or "").strip().lower())
            if ckey in failed_keys:
                failed_rescue_ids.add(c["id"])

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

    # Ledger + feedback: only on a live run (a dry-run must not advance the sweep
    # or skew the per-gene signal). Every judged id is recorded EXCEPT ids whose
    # rescue failed -- those are retried.
    if apply:
        audited = load_ledger(ledger_path) | (plan["judged_ids"] - failed_rescue_ids)
        save_ledger(ledger_path, audited, now=ts)
        if feedback_path:
            update_feedback(feedback_path, plan, fp_candidates, now=ts)

    return {"trashed": trash_result["trashed"], "would_trash": len(plan["to_trash_keys"]),
            "rescued": rescued, "failed_trash": trash_result["failed"],
            "failed_rescue": len(failed_rescue_ids),
            "kept": len(plan["judged_ids"]) - trash_result["trashed"] - rescued,
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

    # Reuse the dedup baseline cached by --prepare (avoids a third full-library
    # fetch this sweep); fall back to a live fetch if the cache is missing.
    baseline = load_dedup_baseline(args.work_dir)
    if baseline is not None:
        existing_pmids, existing_dois, pmid_to_key = baseline
    else:
        pmid_to_key = zot.get_existing_items()   # raises on failure (safety)
        existing_pmids = set(pmid_to_key)
        # Ordering matters: get_existing_dois() returns the dict the preceding
        # get_existing_items() populated; calling it first yields an empty set.
        existing_dois = {d.lower() for d in zot.get_existing_dois()}
    genes_parent_key = zot.get_or_create_collection(args.genes_parent)

    def rescue_fn(entries):
        return run.process_rescue_queue(
            entries, zot, openalex, existing_pmids, existing_dois, pmid_to_key,
            genes_parent_key, additions_tracker=[])

    summary = apply_actions(
        fp, fn, screen, adj, zot=zot, rescue_fn=rescue_fn,
        ledger_path=args.ledger, log_path=args.log, apply=(not args.dry_run),
        feedback_path=args.feedback)
    print(f"apply: trashed={summary['trashed']} would_trash={summary['would_trash']} "
          f"rescued={summary['rescued']} kept={summary['kept']} judged={summary['judged']} "
          f"failed_trash={summary['failed_trash']} failed_rescue={summary['failed_rescue']}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Library audit bot")
    p.add_argument("--data-dir", default=os.path.join(_HERE, "site", "data"))
    p.add_argument("--work-dir", default=os.path.join(_HERE, "audit_work"))
    p.add_argument("--ledger", default=os.path.join(_HERE, "data", "audit_state.json"))
    p.add_argument("--log", default=os.path.join(_HERE, "data", "audit_log.json"))
    p.add_argument("--feedback", default=os.path.join(_HERE, "data", "audit_feedback.json"))
    p.add_argument("--max-items", type=int, default=400)
    # Screener (Haiku) batches: smaller keeps per-item judgment crisp and cuts
    # the "uncertain" rate that inflates the expensive adjudication pass.
    p.add_argument("--batch-size", type=int, default=10)
    # Adjudicator (Sonnet) batches: larger context is fine, fewer agents.
    p.add_argument("--adj-batch-size", type=int, default=20)
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
