"""Post-hoc text filters for PubMed results."""

import re
import logging

logger = logging.getLogger(__name__)


def filter_by_text(
    records: list[dict],
    exclude_terms: list[str] | None = None,
) -> list[dict]:
    """
    Remove records whose title or abstract contains any exclude term.
    Catches papers that slipped through MeSH filters (e.g., not yet indexed).
    """
    if not exclude_terms:
        return records

    patterns = [re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE) for t in exclude_terms]

    kept = []
    removed = 0
    for r in records:
        text = (r.get("title", "") + " " + r.get("abstract", ""))
        if any(p.search(text) for p in patterns):
            removed += 1
            continue
        kept.append(r)

    logger.info(f"Text filter removed {removed} records, {len(kept)} remaining")
    return kept
