"""OpenAlex API client for gene literature discovery.

Uses the OpenAlex Academic Graph API (https://docs.openalex.org/) for both
keyword-based paper search and citation network expansion.

Adaptive selectivity: the more papers already exist in the library for a gene,
the higher the bar (co-citation count or recency) a new candidate must clear.
"""

import time
import logging
import math
from collections import defaultdict

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openalex.org"

# Fields fetched for every work in search results and citation expansion.
# Enough to build a Zotero item without a second API call.
WORK_FIELDS = (
    "id,ids,title,publication_year,publication_date,cited_by_count,"
    "type,primary_location,authorships,referenced_works"
)


def _invert_abstract(inv_index: dict | None) -> str:
    """Reconstruct plain-text abstract from OpenAlex inverted index."""
    if not inv_index:
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inv_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


class OpenAlexClient:
    """Client for the OpenAlex API."""

    def __init__(self, api_key: str | None = None, delay: float = 0.05):
        self.session = requests.Session()
        self.delay = delay
        self.params: dict[str, str] = {}
        if api_key:
            self.params["api_key"] = api_key

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, params: dict | None = None) -> dict | None:
        url = f"{BASE_URL}{endpoint}"
        merged = {**self.params, **(params or {})}
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=merged, timeout=15)
                if resp.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Rate limited (429), waiting {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                time.sleep(self.delay)
                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt < 2:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Request failed ({e}), retrying in {wait}s")
                    time.sleep(wait)
                else:
                    logger.error(f"Request failed after 3 attempts: {e}")
                    return None
        return None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_gene(
        self,
        search_terms: list[str],
        exclude_terms: list[str] | None = None,
        max_results: int = 10000,
    ) -> list[dict]:
        """Search OpenAlex for works mentioning any of the search terms.

        Uses title_and_abstract.search with OR logic for gene symbol + aliases.
        Filters: type=article, has_pmid=true.

        Returns list of work dicts with full metadata.
        """
        query = " OR ".join(search_terms)
        filters = [
            f"title_and_abstract.search:{query}",
            "type:article",
            "has_pmid:true",
        ]
        filter_str = ",".join(filters)

        results: list[dict] = []
        page = 1
        per_page = 200

        while len(results) < max_results:
            data = self._get(
                "/works",
                params={
                    "filter": filter_str,
                    "select": WORK_FIELDS,
                    "sort": "publication_date:desc",
                    "per_page": str(per_page),
                    "page": str(page),
                },
            )
            if not data or "results" not in data:
                break
            batch = data["results"]
            if not batch:
                break
            results.extend(batch)
            total = data.get("meta", {}).get("count", 0)
            if total > max_results:
                logger.warning(
                    f"Search returned {total} results, capping at {max_results}"
                )
            if len(batch) < per_page:
                break
            page += 1

        logger.info(f"OpenAlex search: {len(results)} works found")

        # Post-hoc text exclusion
        if exclude_terms:
            before = len(results)
            results = self._filter_by_text(results, exclude_terms)
            logger.info(f"Text filter removed {before - len(results)} works")

        return results[:max_results]

    # ------------------------------------------------------------------
    # Citation network expansion
    # ------------------------------------------------------------------

    def get_citations(self, openalex_id: str, limit: int = 500) -> list[dict]:
        """Get works that cite the given work (forward citations)."""
        results = []
        page = 1
        per_page = min(limit, 200)

        while len(results) < limit:
            data = self._get(
                "/works",
                params={
                    "filter": f"cites:{openalex_id},has_pmid:true",
                    "select": "id,ids,cited_by_count,publication_year",
                    "per_page": str(per_page),
                    "page": str(page),
                },
            )
            if not data or "results" not in data:
                break
            batch = data["results"]
            if not batch:
                break
            results.extend(batch)
            if len(batch) < per_page:
                break
            page += 1

        return results[:limit]

    def expand_citations(
        self,
        seed_works: list[dict],
        existing_pmids: set[str],
        library_size: int,
        max_seeds: int = 100,
        min_co_citations: int = 1,
    ) -> list[dict]:
        """Expand seed papers via one-hop citation network.

        Adaptive selectivity: the larger the existing library for this gene,
        the higher the bar for inclusion:
        - min_co_citations scales with log2(library_size)
        - candidates also get a recency bonus (recent papers need fewer
          co-citations to pass)

        Returns candidate PMIDs sorted by score, with full work metadata
        fetched for the top candidates.
        """
        if not seed_works:
            return []

        # Adaptive thresholds based on library size
        adaptive_min_co = max(
            min_co_citations,
            int(math.log2(max(library_size, 2)))
        )
        logger.info(
            f"Citation expansion: {len(seed_works)} seeds, "
            f"library_size={library_size}, adaptive_min_co={adaptive_min_co}"
        )

        # Keep the most-cited seeds for expansion
        seeds = sorted(
            seed_works,
            key=lambda w: w.get("cited_by_count", 0),
            reverse=True,
        )[:max_seeds]

        seed_pmids = {self._extract_pmid(w) for w in seed_works}

        # candidate OpenAlex ID -> {co_citations, directions, year}
        candidates: dict[str, dict] = defaultdict(
            lambda: {"co_citations": 0, "directions": set(), "year": 0}
        )

        for i, work in enumerate(seeds):
            openalex_id = work.get("id", "")
            if (i + 1) % 20 == 0 or (i + 1) == len(seeds):
                logger.info(f"Expanding seed {i + 1}/{len(seeds)}")

            # Backward: referenced_works already in the work object
            for ref_id in work.get("referenced_works", []):
                candidates[ref_id]["co_citations"] += 1
                candidates[ref_id]["directions"].add("reference")

            # Forward: query works that cite this seed
            citing_works = self.get_citations(openalex_id, limit=500)
            for citing in citing_works:
                cit_id = citing.get("id", "")
                if cit_id:
                    candidates[cit_id]["co_citations"] += 1
                    candidates[cit_id]["directions"].add("citation")
                    year = citing.get("publication_year", 0) or 0
                    if year > candidates[cit_id]["year"]:
                        candidates[cit_id]["year"] = year

        logger.info(f"Raw candidates from citation network: {len(candidates)}")

        # Resolve OpenAlex IDs to PMIDs (only those with PMIDs)
        candidate_ids = list(candidates.keys())
        id_to_pmid = self._resolve_openalex_ids_to_pmids(candidate_ids)
        logger.info(f"Candidates with PMIDs: {len(id_to_pmid)}")

        # Score, filter, sort
        current_year = time.localtime().tm_year
        scored: list[dict] = []

        for oa_id, pmid in id_to_pmid.items():
            if pmid in existing_pmids or pmid in seed_pmids:
                continue

            info = candidates[oa_id]
            co_cit = info["co_citations"]
            year = info["year"]

            # Recency bonus: papers from the last 3 years get a lower bar
            recency_bonus = max(0, 3 - (current_year - year)) if year else 0
            effective_co = co_cit + recency_bonus

            if effective_co < adaptive_min_co:
                continue

            dirs = info["directions"]
            if "reference" in dirs and "citation" in dirs:
                direction = "both"
            elif "citation" in dirs:
                direction = "citation"
            else:
                direction = "reference"

            scored.append({
                "pmid": pmid,
                "openalex_id": oa_id,
                "co_citations": co_cit,
                "recency_bonus": recency_bonus,
                "effective_score": effective_co,
                "year": year,
                "direction": direction,
            })

        scored.sort(key=lambda x: x["effective_score"], reverse=True)

        logger.info(
            f"Citation expansion: {len(seeds)} seeds -> {len(scored)} candidates "
            f"above threshold (adaptive_min_co={adaptive_min_co})"
        )

        return scored

    def fetch_works_by_pmids(self, pmids: list[str]) -> list[dict]:
        """Fetch full work metadata for a list of PMIDs.

        Returns list of work dicts with all fields needed for Zotero upload.
        """
        result: list[dict] = []
        batch_size = 50

        for i in range(0, len(pmids), batch_size):
            batch = pmids[i:i + batch_size]
            pmid_filter = "|".join(batch)
            page = 1
            while True:
                data = self._get(
                    "/works",
                    params={
                        "filter": f"pmid:{pmid_filter}",
                        "select": WORK_FIELDS,
                        "per_page": "200",
                        "page": str(page),
                    },
                )
                if not data or "results" not in data:
                    break
                result.extend(data["results"])
                if len(data["results"]) < 200:
                    break
                page += 1

        logger.info(f"Fetched {len(result)} works by PMID from OpenAlex")
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_openalex_ids_to_pmids(
        self, openalex_ids: list[str],
    ) -> dict[str, str]:
        """Resolve a list of OpenAlex IDs to PMIDs.

        Returns {openalex_id: pmid} for works that have a PMID.
        """
        result: dict[str, str] = {}
        batch_size = 50

        for i in range(0, len(openalex_ids), batch_size):
            batch = openalex_ids[i:i + batch_size]
            id_filter = "|".join(batch)
            page = 1
            while True:
                data = self._get(
                    "/works",
                    params={
                        "filter": f"ids.openalex:{id_filter},has_pmid:true",
                        "select": "id,ids",
                        "per_page": "200",
                        "page": str(page),
                    },
                )
                if not data or "results" not in data:
                    break
                for work in data["results"]:
                    pmid = self._extract_pmid(work)
                    oa_id = work.get("id", "")
                    if pmid and oa_id:
                        result[oa_id] = pmid
                if len(data["results"]) < 200:
                    break
                page += 1

        return result

    @staticmethod
    def _filter_by_text(works: list[dict], exclude_terms: list[str]) -> list[dict]:
        """Remove works whose title contains any exclude term."""
        import re
        patterns = [
            re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE)
            for t in exclude_terms
        ]
        kept = []
        for w in works:
            title = w.get("title", "") or ""
            if any(p.search(title) for p in patterns):
                continue
            kept.append(w)
        return kept

    @staticmethod
    def _extract_pmid(work: dict) -> str | None:
        ids = work.get("ids", {})
        pmid = ids.get("pmid", "")
        if pmid:
            # OpenAlex returns "https://pubmed.ncbi.nlm.nih.gov/12345678"
            return pmid.rstrip("/").split("/")[-1]
        return None

    @staticmethod
    def work_to_record(work: dict) -> dict:
        """Convert an OpenAlex work dict to a flat record for Zotero upload.

        Produces the same keys that zotero_client.add_papers() expects.
        """
        ids = work.get("ids", {})
        pmid = ""
        raw_pmid = ids.get("pmid", "")
        if raw_pmid:
            pmid = raw_pmid.rstrip("/").split("/")[-1]

        doi = ""
        raw_doi = ids.get("doi", "")
        if raw_doi:
            doi = raw_doi.replace("https://doi.org/", "")

        # Journal info from primary_location
        loc = work.get("primary_location") or {}
        source = loc.get("source") or {}
        journal = source.get("display_name", "")

        # Authors
        authors = []
        for authorship in work.get("authorships", []):
            author = authorship.get("author", {})
            name = author.get("display_name", "")
            if name:
                authors.append(name)

        return {
            "pmid": pmid,
            "doi": doi,
            "title": work.get("title", ""),
            "authors": authors,
            "journal": journal,
            "journal_abbr": "",
            "year": str(work.get("publication_year", "")),
            "date_published": work.get("publication_date", ""),
            "abstract": "",
            "publication_type": [work.get("type", "")],
            "volume": "",
            "issue": "",
            "pages": "",
            "cited_by_count": work.get("cited_by_count", 0),
        }
