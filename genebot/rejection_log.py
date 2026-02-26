"""Rejection log for near-miss articles dashboard.

Collects articles rejected by the pipeline at various filter stages,
with metadata and rejection reason. Writes JSON for the GitHub Pages
dashboard to display.

Cumulative mode: when a previous JSON file is provided, new rejections
are merged with existing entries. Articles are deduplicated by PMID
(fallback to DOI), with first_seen/last_seen/seen_count tracking.
"""

import json
import logging
import os
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
        """Build category -> subcollection list from entries.

        Handles comma-separated values from merged entries (an article
        rejected in multiple genes/categories).
        """
        hierarchy: dict[str, set[str]] = {}
        for e in self.entries:
            cats = [c.strip() for c in e.get("category", "").split(",") if c.strip()]
            subs = [s.strip() for s in e.get("subcollection", "").split(",") if s.strip()]
            for cat in cats:
                for sub in subs:
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

    def _dedup_key(self, entry: dict) -> str | None:
        """Return a dedup key for an entry: PMID first, DOI fallback, or None."""
        pmid = entry.get("pmid", "").strip()
        if pmid:
            return f"pmid:{pmid}"
        doi = entry.get("doi", "").strip()
        if doi:
            return f"doi:{doi}"
        return None

    def _merge_subcollections(self, existing: str, new: str) -> str:
        """Merge subcollection values, comma-separated if they differ."""
        if not existing:
            return new
        if not new or new == existing:
            return existing
        parts = {s.strip() for s in existing.split(",")}
        parts.add(new.strip())
        return ", ".join(sorted(parts))

    def _merge_categories(self, existing: str, new: str) -> str:
        """Merge category values, comma-separated if they differ."""
        if not existing:
            return new
        if not new or new == existing:
            return existing
        parts = {s.strip() for s in existing.split(",")}
        parts.add(new.strip())
        return ", ".join(sorted(parts))

    def _load_previous(self, previous_path: str) -> tuple[dict, int]:
        """Load previous JSON data. Returns (data_dict, pipeline_runs)."""
        if not previous_path or not os.path.isfile(previous_path):
            return {}, 0
        try:
            with open(previous_path, "r", encoding="utf-8") as f:
                prev = json.load(f)
            if not isinstance(prev, dict) or "articles" not in prev:
                logger.warning(
                    f"Previous data at {previous_path} has no 'articles' key, "
                    f"starting fresh"
                )
                return {}, 0
            pipeline_runs = prev.get("pipeline_runs", 0)
            return prev, pipeline_runs
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not load previous data from {previous_path}: {e}")
            return {}, 0

    def to_json(self, path: str, previous_path: str | None = None) -> None:
        """Write all entries to JSON file, optionally merging with previous data.

        If previous_path is provided and exists, loads previous entries and
        merges: existing articles keep their first_seen, get updated last_seen
        and incremented seen_count. New articles start with seen_count=1.
        Articles from previous data not seen this run are preserved as-is.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Build index of previous entries
        prev_data, prev_runs = self._load_previous(previous_path)
        prev_index: dict[str, dict] = {}
        if prev_data:
            for article in prev_data.get("articles", []):
                key = self._dedup_key(article)
                if key:
                    prev_index[key] = article

        # Merge new entries with previous
        merged_index: dict[str, dict] = {}
        seen_keys: set[str] = set()

        for entry in self.entries:
            key = self._dedup_key(entry)
            if key is None:
                # No PMID or DOI -- cannot merge, just add with defaults
                entry.setdefault("first_seen", now)
                entry["last_seen"] = now
                entry["seen_count"] = 1
                # Use a unique fallback key
                fallback = f"_no_id_{id(entry)}"
                merged_index[fallback] = entry
                continue

            seen_keys.add(key)

            if key in prev_index:
                # Existing article: keep first_seen, update rest
                existing = prev_index[key]
                entry["first_seen"] = existing.get("first_seen", now)
                entry["last_seen"] = now
                entry["seen_count"] = existing.get("seen_count", 1) + 1
                # Merge subcollection/category if they differ
                entry["subcollection"] = self._merge_subcollections(
                    existing.get("subcollection", ""),
                    entry.get("subcollection", ""),
                )
                entry["category"] = self._merge_categories(
                    existing.get("category", ""),
                    entry.get("category", ""),
                )
            else:
                # New article
                entry["first_seen"] = now
                entry["last_seen"] = now
                entry["seen_count"] = 1

            # If same key appears multiple times in this run, keep highest seen_count
            if key in merged_index:
                prev_count = merged_index[key].get("seen_count", 1)
                if entry["seen_count"] >= prev_count:
                    entry["subcollection"] = self._merge_subcollections(
                        merged_index[key].get("subcollection", ""),
                        entry.get("subcollection", ""),
                    )
                    entry["category"] = self._merge_categories(
                        merged_index[key].get("category", ""),
                        entry.get("category", ""),
                    )
                    merged_index[key] = entry
            else:
                merged_index[key] = entry

        # Carry forward previous articles not seen this run
        for key, article in prev_index.items():
            if key not in seen_keys:
                # Ensure cumulative fields exist on carried-forward entries
                article.setdefault("first_seen", now)
                article.setdefault("last_seen", article["first_seen"])
                article.setdefault("seen_count", 1)
                if key not in merged_index:
                    merged_index[key] = article

        # Final merged list
        output_entries = list(merged_index.values())

        # Rebuild hierarchy and stats from merged set (without mutating self.entries)
        pipeline_runs = prev_runs + 1

        def _build_stats_from(entries: list) -> dict:
            by_reason: dict[str, int] = {}
            by_category: dict[str, int] = {}
            for e in entries:
                reason = e.get("reason", "unknown")
                by_reason[reason] = by_reason.get(reason, 0) + 1
                cat = e.get("category", "unknown")
                by_category[cat] = by_category.get(cat, 0) + 1
            return {
                "total_rejections": len(entries),
                "by_reason": by_reason,
                "by_category": by_category,
            }

        def _build_hierarchy_from(entries: list) -> dict:
            hierarchy: dict[str, set] = {}
            for e in entries:
                cats = [c.strip() for c in e.get("category", "").split(",") if c.strip()]
                subs = [s.strip() for s in e.get("subcollection", "").split(",") if s.strip()]
                for cat in cats:
                    for sub in subs:
                        hierarchy.setdefault(cat, set()).add(sub)
            return {cat: sorted(subs) for cat, subs in sorted(hierarchy.items())}

        data = {
            "generated_at": now,
            "pipeline_version": "1.0",
            "pipeline_runs": pipeline_runs,
            "stats": _build_stats_from(output_entries),
            "hierarchy": _build_hierarchy_from(output_entries),
            "articles": output_entries,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

        new_count = sum(1 for a in output_entries if a.get("seen_count", 1) == 1)
        recurring_count = sum(1 for a in output_entries if a.get("seen_count", 1) > 1)
        logger.info(
            f"Rejection log written: {len(output_entries)} entries "
            f"({new_count} new, {recurring_count} recurring, "
            f"run #{pipeline_runs}) -> {path}"
        )
