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

# The cumulative near_misses.json is copied to site/data/ and pushed to the
# gh-pages branch, where GitHub enforces a hard 100 MB per-file blob limit.
# Left unbounded the file grows every run (abstracts dominate its size) and
# eventually crosses that limit, at which point the dashboard deploy push is
# rejected outright. Bound the retained set so the written file can never cross
# it. Both limits are overridable via env for tuning; MAX_BYTES governs the
# hard guarantee, MAX_ARTICLES is a secondary cap that also keeps the
# single-page dashboard from having to load an unreasonable number of rows.
MAX_ARTICLES = int(os.environ.get("NEAR_MISS_MAX_ARTICLES", "50000"))
# 85 MB target leaves comfortable headroom under GitHub's 100 MB limit for the
# JSON wrapper (stats/hierarchy), indentation, and estimation slack.
MAX_BYTES = int(os.environ.get("NEAR_MISS_MAX_BYTES", str(85 * 1024 * 1024)))
# Guard against pathologically long abstracts (some OpenAlex records run to tens
# of KB); real abstracts sit well under this, so only outliers get trimmed.
MAX_ABSTRACT_CHARS = 8000


class RejectionLog:
    """Accumulates rejected articles during a pipeline run."""

    def __init__(self):
        self.entries: list[dict] = []
        self._subcollection: str = ""
        self._category: str = ""
        self._search_keywords: list[str] = []

    def set_context(
        self,
        subcollection: str,
        category: str,
        search_keywords: list[str] | None = None,
    ) -> None:
        """Set the current subcollection/category for subsequent add() calls."""
        self._subcollection = subcollection
        self._category = category
        self._search_keywords = list(search_keywords) if search_keywords else []

    def add_from_work(
        self,
        work: dict,
        reason: str,
        matched_term: str | None = None,
        scores: dict | None = None,
    ) -> None:
        """Record a rejection from a raw OpenAlex work dict.

        Used by the citation-expansion path (bio_toolkit ``expand_citations``),
        which passes raw works plus optional scoring metadata. The discovery
        passes use ``add_from_record`` with a flat ``work_to_record()`` dict.
        """
        from bio_toolkit.clients.openalex import OpenAlexClient, _invert_abstract

        pmid = OpenAlexClient.extract_pmid(work) or ""
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
            "search_keywords": list(self._search_keywords),
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
            "search_keywords": list(self._search_keywords),
        })

    @staticmethod
    def _build_hierarchy(entries: list[dict]) -> dict[str, list[str]]:
        """Build category -> subcollection list from entries.

        Handles comma-separated values from merged entries (an article
        rejected in multiple genes/categories).
        """
        hierarchy: dict[str, set[str]] = {}
        for e in entries:
            for cat, sub in RejectionLog._context_pairs(e):
                hierarchy.setdefault(cat, set()).add(sub)
        return {cat: sorted(subs) for cat, subs in sorted(hierarchy.items())}

    @staticmethod
    def _prune(entries: list[dict]) -> list[dict]:
        """Bound the retained set so the written file stays under GitHub's limit.

        Entries are ranked by usefulness for rescue triage -- most recently seen
        first, then recurring (higher seen_count), then most cited -- and the
        lowest-value tail is dropped once either ``MAX_ARTICLES`` or the
        ``MAX_BYTES`` serialized-size budget is reached. Pathologically long
        abstracts are trimmed to ``MAX_ABSTRACT_CHARS`` so a few outliers can't
        crowd out the rest of the corpus.

        Returns a new list; the input entries are not mutated.
        """
        def rank(e: dict) -> tuple:
            return (
                e.get("last_seen", "") or e.get("first_seen", ""),
                e.get("seen_count", 1),
                e.get("cited_by_count", 0),
            )

        ordered = sorted(entries, key=rank, reverse=True)

        kept: list[dict] = []
        total = 0
        for e in ordered:
            abstract = e.get("abstract") or ""
            if len(abstract) > MAX_ABSTRACT_CHARS:
                e = dict(e)
                e["abstract"] = abstract[:MAX_ABSTRACT_CHARS] + "..."
            # indent=1 mirrors the on-disk formatting closely enough to keep the
            # estimate on the safe side; +2 covers the item separator/newline.
            size = len(json.dumps(e, ensure_ascii=False, indent=1).encode("utf-8")) + 2
            if kept and (len(kept) >= MAX_ARTICLES or total + size > MAX_BYTES):
                break
            kept.append(e)
            total += size

        dropped = len(entries) - len(kept)
        if dropped > 0:
            logger.warning(
                f"Near-miss log pruned: kept {len(kept)} of {len(entries)} "
                f"entries ({dropped} dropped) to stay under the "
                f"{MAX_BYTES // (1024 * 1024)} MB / {MAX_ARTICLES}-entry budget"
            )
        return kept

    @staticmethod
    def _build_stats(entries: list[dict]) -> dict:
        """Compute summary statistics."""
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

    def _dedup_key(self, entry: dict) -> str | None:
        """Return a dedup key for an entry: PMID first, DOI fallback, or None."""
        pmid = entry.get("pmid", "").strip()
        if pmid:
            return f"pmid:{pmid}"
        doi = entry.get("doi", "").strip()
        if doi:
            return f"doi:{doi}"
        return None

    @staticmethod
    def _context_pairs(entry: dict) -> list[tuple[str, str]]:
        """Return an entry's (category, subcollection) pairs.

        The two fields are parallel comma-separated lists: the Nth category is
        the parent of the Nth subcollection. Any unpaired tail (only possible in
        data written before the pair-aware merge landed) is dropped rather than
        guessed at -- a wrong pairing creates real gene collections under topic
        categories via the rescue queue.
        """
        cats = [c.strip() for c in (entry.get("category") or "").split(",") if c.strip()]
        subs = [s.strip() for s in (entry.get("subcollection") or "").split(",") if s.strip()]
        return list(zip(cats, subs))

    @staticmethod
    def _write_context_pairs(entry: dict, pairs) -> None:
        """Write (category, subcollection) pairs back as two aligned CSV fields."""
        ordered = sorted(dict.fromkeys(pairs))
        entry["category"] = ", ".join(cat for cat, _ in ordered)
        entry["subcollection"] = ", ".join(sub for _, sub in ordered)

    def _merge_context(self, entry: dict, previous: dict) -> None:
        """Merge the collection context of ``previous`` into ``entry``.

        Merging must happen on (category, subcollection) *pairs*: deduplicating
        and sorting the two fields independently desynchronises them, so a
        near-miss shared by a gene and a topic ends up claiming e.g. subcollection
        ABCA4 under category '1 - Anatomie'.
        """
        pairs = self._context_pairs(previous) + self._context_pairs(entry)
        self._write_context_pairs(entry, pairs)

    def _merge_search_keywords(
        self, existing: list | None, new: list | None
    ) -> list[str]:
        """Merge search_keywords lists (union, sorted)."""
        combined = set(existing or [])
        combined.update(new or [])
        return sorted(combined)

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

        for orig_entry in self.entries:
            entry = dict(orig_entry)  # shallow copy to avoid mutating self.entries
            key = self._dedup_key(entry)
            if key is None:
                # No PMID or DOI -- cannot merge, just add with defaults
                entry.setdefault("first_seen", now)
                entry["last_seen"] = now
                entry["seen_count"] = 1
                # Use a unique fallback key
                fallback = f"_no_id_{id(orig_entry)}"
                merged_index[fallback] = entry
                continue

            seen_keys.add(key)

            if key in prev_index:
                # Existing article: keep first_seen, update rest
                existing = prev_index[key]
                entry["first_seen"] = existing.get("first_seen", now)
                entry["last_seen"] = now
                entry["seen_count"] = existing.get("seen_count", 1) + 1
                # Merge subcollection/category as pairs if they differ
                self._merge_context(entry, existing)
                entry["search_keywords"] = self._merge_search_keywords(
                    existing.get("search_keywords"),
                    entry.get("search_keywords"),
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
                    self._merge_context(entry, merged_index[key])
                    entry["search_keywords"] = self._merge_search_keywords(
                        merged_index[key].get("search_keywords"),
                        entry.get("search_keywords"),
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
                # Normalise the collection context as pairs (dedup + stable order).
                # Must not sort the two fields independently -- that is exactly
                # what desynchronised them in the first place.
                self._write_context_pairs(article, self._context_pairs(article))
                if key not in merged_index:
                    merged_index[key] = article

        # Final merged list, bounded so the written file stays under GitHub's
        # 100 MB blob limit (the gh-pages deploy push is rejected above it).
        output_entries = self._prune(list(merged_index.values()))

        # Rebuild hierarchy and stats from merged set (without mutating self.entries)
        pipeline_runs = prev_runs + 1

        data = {
            "generated_at": now,
            "pipeline_version": "1.0",
            "pipeline_runs": pipeline_runs,
            "stats": self._build_stats(output_entries),
            "hierarchy": self._build_hierarchy(output_entries),
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
