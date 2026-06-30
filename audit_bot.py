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


def chunk(items: list, size: int) -> list:
    """Split items into sublists of at most `size` elements."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
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
        try:
            with open(os.path.join(bdir, fname), encoding="utf-8") as f:
                items = json.load(f).get("items", [])
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("load_batch_items: skipping unreadable %s: %s", fname, e)
            continue
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
