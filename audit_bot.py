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
