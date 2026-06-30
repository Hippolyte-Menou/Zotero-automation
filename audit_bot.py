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
        })
    return out


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
