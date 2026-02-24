"""Rejection log for near-miss articles dashboard.

Collects articles rejected by the pipeline at various filter stages,
with metadata and rejection reason. Writes JSON for the GitHub Pages
dashboard to display.
"""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class RejectionLog:
    """Accumulates rejected articles during a pipeline run."""

    def __init__(self):
        self.entries: list[dict] = []
        self._subcollection: str = ""
        self._category: str = ""

    def set_context(self, subcollection: str, category: str) -> None:
        """Set the current subcollection/category for subsequent add() calls."""
        self._subcollection = subcollection
        self._category = category

    def add_from_work(
        self,
        work: dict,
        reason: str,
        matched_term: str | None = None,
        scores: dict | None = None,
    ) -> None:
        """Record a rejection from a raw OpenAlex work dict."""
        from genebot.openalex import OpenAlexClient, _invert_abstract

        pmid = OpenAlexClient._extract_pmid(work) or ""
        ids = work.get("ids", {})
        raw_doi = ids.get("doi", "")
        doi = raw_doi.replace("https://doi.org/", "") if raw_doi else ""

        authors = []
        for authorship in work.get("authorships", []):
            author = authorship.get("author", {})
            display = author.get("display_name", "") or ""
            raw = authorship.get("raw_author_name", "") or ""
            if "," in raw:
                last, _, first = raw.partition(",")
                authors.append([first.strip(), last.strip()])
            elif display:
                parts = display.rsplit(" ", 1)
                if len(parts) == 2:
                    authors.append([parts[0], parts[1]])
                else:
                    authors.append(["", display])

        loc = work.get("primary_location") or {}
        source = loc.get("source") or {}

        entry = {
            "pmid": pmid,
            "doi": doi,
            "title": work.get("title", "") or "",
            "authors": authors,
            "journal": source.get("display_name", ""),
            "year": str(work.get("publication_year", "")),
            "abstract": _invert_abstract(work.get("abstract_inverted_index")),
            "cited_by_count": work.get("cited_by_count", 0),
            "reason": reason,
            "subcollection": self._subcollection,
            "category": self._category,
            "matched_term": matched_term,
        }
        if scores:
            entry.update({
                "co_citations": scores.get("co_citations"),
                "bib_coupling": scores.get("bib_coupling"),
                "recency_bonus": scores.get("recency_bonus"),
                "effective_score": scores.get("effective_score"),
                "threshold": scores.get("threshold"),
                "direction": scores.get("direction"),
            })
        self.entries.append(entry)

    def add_from_record(
        self,
        record: dict,
        reason: str,
        matched_term: str | None = None,
    ) -> None:
        """Record a rejection from a flat work_to_record() dict."""
        authors = [
            list(a) if isinstance(a, (list, tuple)) else a
            for a in record.get("authors", [])
        ]
        self.entries.append({
            "pmid": record.get("pmid", ""),
            "doi": record.get("doi", ""),
            "title": record.get("title", ""),
            "authors": authors,
            "journal": record.get("journal", ""),
            "year": record.get("year", ""),
            "abstract": record.get("abstract", ""),
            "cited_by_count": record.get("cited_by_count", 0),
            "reason": reason,
            "subcollection": self._subcollection,
            "category": self._category,
            "matched_term": matched_term,
        })

    def build_hierarchy(self) -> dict[str, list[str]]:
        """Build category -> subcollection list from entries."""
        hierarchy: dict[str, set[str]] = {}
        for e in self.entries:
            cat = e.get("category", "")
            sub = e.get("subcollection", "")
            if cat and sub:
                hierarchy.setdefault(cat, set()).add(sub)
        return {cat: sorted(subs) for cat, subs in sorted(hierarchy.items())}

    def build_stats(self) -> dict:
        """Compute summary statistics."""
        by_reason: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for e in self.entries:
            reason = e.get("reason", "unknown")
            by_reason[reason] = by_reason.get(reason, 0) + 1
            cat = e.get("category", "unknown")
            by_category[cat] = by_category.get(cat, 0) + 1
        return {
            "total_rejections": len(self.entries),
            "by_reason": by_reason,
            "by_category": by_category,
        }

    def to_json(self, path: str) -> None:
        """Write all entries to JSON file."""
        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pipeline_version": "1.0",
            "stats": self.build_stats(),
            "hierarchy": self.build_hierarchy(),
            "articles": self.entries,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        logger.info(
            f"Rejection log written: {len(self.entries)} entries -> {path}"
        )
