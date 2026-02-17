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
    "type,primary_location,authorships,referenced_works,"
    "abstract_inverted_index"
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


def _tags_to_keywords(tags: list[str]) -> list[str]:
    """Convert genes.yml tag slugs to OpenAlex search keywords.

    Replaces hyphens with spaces. Multi-word terms are quoted for
    exact phrase matching in OpenAlex boolean search.
    """
    keywords = []
    for tag in tags:
        term = tag.replace("-", " ")
        if " " in term:
            term = f'"{term}"'
        keywords.append(term)
    return keywords


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
        disease_keywords: list[str] | None = None,
    ) -> list[dict]:
        """Search OpenAlex for works mentioning any of the search terms.

        Uses title_and_abstract.search with boolean logic:
        - Without disease_keywords: OR logic for gene symbol + aliases
        - With disease_keywords: (gene terms) AND (disease terms)

        Filters: type=article, has_pmid=true.

        Returns list of work dicts with full metadata.
        """
        gene_part = " OR ".join(search_terms)
        if disease_keywords:
            kw_terms = _tags_to_keywords(disease_keywords)
            disease_part = " OR ".join(kw_terms)
            query = f"({gene_part}) AND ({disease_part})"
        else:
            query = gene_part
        logger.info(f"OpenAlex query: {query}")
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

    def search_gene_recent(
        self,
        search_terms: list[str],
        disease_keywords: list[str] | None = None,
        year: int | None = None,
        max_results: int = 10,
    ) -> list[dict]:
        """Search OpenAlex for recent papers (current year by default).

        Same boolean query logic as search_gene() but filtered to papers
        published from `year-01-01` onwards. Used as a bypass pass to
        catch new publications before they accumulate enough citations.

        Returns list of work dicts with full metadata.
        """
        import datetime
        if year is None:
            year = datetime.date.today().year

        gene_part = " OR ".join(search_terms)
        if disease_keywords:
            kw_terms = _tags_to_keywords(disease_keywords)
            disease_part = " OR ".join(kw_terms)
            query = f"({gene_part}) AND ({disease_part})"
        else:
            query = gene_part

        date_filter = f"{year}-01-01"
        logger.info(
            f"OpenAlex recent-papers query (from {date_filter}): {query}"
        )

        filters = [
            f"title_and_abstract.search:{query}",
            "type:article",
            "has_pmid:true",
            f"from_publication_date:{date_filter}",
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
            if len(batch) < per_page:
                break
            page += 1

        logger.info(f"OpenAlex recent-papers search: {len(results)} works found")
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

    def _expand_one_hop(
        self,
        seed_works: list[dict],
        existing_pmids: set[str],
        library_size: int,
        max_seeds: int = 100,
        min_co_citations: int = 1,
        max_min_co: int = 6,
        hop_label: str = "hop 1",
    ) -> list[dict]:
        """Single-hop citation network expansion with bibliographic coupling.

        Scores candidates by:
        - co_citations: how many seeds cite or are cited by this candidate
        - bib_coupling: how many references the candidate shares with the
          seed set (capped at 3 to avoid over-weighting review papers)
        - recency_bonus: recent papers (< 3 years) get up to +3

        Returns scored candidate list sorted by effective_score.
        """
        if not seed_works:
            return []

        # Adaptive threshold: scales with log2(library_size), capped at max_min_co
        # to prevent mature genes from freezing out new discoveries.
        adaptive_min_co = min(
            max_min_co,
            max(min_co_citations, int(math.log2(max(library_size, 2))))
        )
        logger.info(
            f"Citation expansion ({hop_label}): {len(seed_works)} seeds, "
            f"library_size={library_size}, adaptive_min_co={adaptive_min_co} "
            f"(cap={max_min_co})"
        )

        # Keep the most-cited seeds for expansion
        seeds = sorted(
            seed_works,
            key=lambda w: w.get("cited_by_count", 0),
            reverse=True,
        )[:max_seeds]

        seed_pmids = {self._extract_pmid(w) for w in seed_works}

        # Build seed reference set for bibliographic coupling
        seed_reference_set: set[str] = set()
        for w in seeds:
            seed_reference_set.update(w.get("referenced_works", []))

        # candidate OpenAlex ID -> {co_citations, bib_coupling, directions, year, referenced_works}
        candidates: dict[str, dict] = defaultdict(
            lambda: {"co_citations": 0, "bib_coupling": 0, "directions": set(), "year": 0}
        )

        for i, work in enumerate(seeds):
            openalex_id = work.get("id", "")
            if (i + 1) % 20 == 0 or (i + 1) == len(seeds):
                logger.info(f"Expanding seed {i + 1}/{len(seeds)} ({hop_label})")

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

        logger.info(f"Raw candidates from citation network ({hop_label}): {len(candidates)}")

        # Resolve OpenAlex IDs to PMIDs (only those with PMIDs)
        candidate_ids = list(candidates.keys())
        id_to_pmid = self._resolve_openalex_ids_to_pmids(candidate_ids)
        logger.info(f"Candidates with PMIDs ({hop_label}): {len(id_to_pmid)}")

        # Score, filter, sort
        current_year = time.localtime().tm_year
        scored: list[dict] = []

        for oa_id, pmid in id_to_pmid.items():
            if pmid in existing_pmids or pmid in seed_pmids:
                continue

            info = candidates[oa_id]
            co_cit = info["co_citations"]
            year = info["year"]

            # Bibliographic coupling: count shared references with seed set
            # (candidates from forward citations won't have referenced_works
            # in the lightweight data -- bib_coupling stays 0 for them here,
            # it will be computed after full metadata fetch in expand_citations)
            bib_coupling = info["bib_coupling"]

            # Recency bonus: papers from the last 3 years get a lower bar
            recency_bonus = max(0, 3 - (current_year - year)) if year else 0
            bib_bonus = min(bib_coupling, 3)
            effective_co = co_cit + bib_bonus + recency_bonus

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
                "bib_coupling": bib_coupling,
                "recency_bonus": recency_bonus,
                "effective_score": effective_co,
                "year": year,
                "direction": direction,
            })

        scored.sort(key=lambda x: x["effective_score"], reverse=True)

        logger.info(
            f"Citation expansion ({hop_label}): {len(seeds)} seeds -> "
            f"{len(scored)} candidates above threshold "
            f"(adaptive_min_co={adaptive_min_co})"
        )

        return scored

    def expand_citations(
        self,
        seed_works: list[dict],
        existing_pmids: set[str],
        library_size: int,
        max_seeds: int = 100,
        min_co_citations: int = 1,
        max_min_co: int = 6,
        gene_terms: list[str] | None = None,
        max_hops: int = 1,
        hop2_top_n: int = 10,
    ) -> list[dict]:
        """Multi-hop citation expansion with gene-name filtering and
        bibliographic coupling.

        Hop 1: expand seed_works via citation network.
        Hop 2 (if max_hops >= 2): top candidates from hop 1 become new seeds.

        After each hop, candidates are filtered to only keep papers that
        mention the gene (symbol or aliases) in their title or abstract.

        Returns final scored candidate list with full work metadata attached
        (key 'work' on each candidate dict).
        """
        if not seed_works:
            return []

        all_seen_pmids = set(existing_pmids)
        all_candidates: list[dict] = []

        for hop in range(1, max_hops + 1):
            hop_label = f"hop {hop}"

            if hop == 1:
                hop_seeds = seed_works
            else:
                # Use top candidates from previous hop as new seeds
                if not all_candidates:
                    logger.info(f"No candidates from previous hop, skipping {hop_label}")
                    break
                top_pmids = [c["pmid"] for c in all_candidates[:hop2_top_n]]
                hop_seed_works = self.fetch_works_by_pmids(top_pmids)
                if not hop_seed_works:
                    logger.info(f"Could not fetch seed works for {hop_label}")
                    break
                hop_seeds = hop_seed_works
                logger.info(
                    f"{hop_label}: using top {len(hop_seeds)} candidates as seeds"
                )

            scored = self._expand_one_hop(
                seed_works=hop_seeds,
                existing_pmids=all_seen_pmids,
                library_size=library_size,
                max_seeds=max_seeds,
                min_co_citations=min_co_citations,
                max_min_co=max_min_co,
                hop_label=hop_label,
            )

            if not scored:
                logger.info(f"No candidates from {hop_label}")
                break

            # Fetch full metadata for candidates
            candidate_pmids = [c["pmid"] for c in scored]
            candidate_works = self.fetch_works_by_pmids(candidate_pmids)
            work_by_pmid = {self._extract_pmid(w): w for w in candidate_works}

            # Compute bibliographic coupling from full metadata
            # Build seed reference set
            seed_ref_set: set[str] = set()
            for w in hop_seeds:
                seed_ref_set.update(w.get("referenced_works", []))

            for c in scored:
                work = work_by_pmid.get(c["pmid"])
                if not work:
                    continue
                refs = set(work.get("referenced_works", []))
                bib_coupling = len(refs & seed_ref_set)
                c["bib_coupling"] = bib_coupling
                bib_bonus = min(bib_coupling, 3)
                c["effective_score"] = (
                    c["co_citations"] + bib_bonus + c["recency_bonus"]
                )
                c["work"] = work

            # Remove candidates without full metadata
            scored = [c for c in scored if "work" in c]

            # Gene-name filter
            if gene_terms:
                before = len(scored)
                scored = [
                    c for c in scored
                    if self._mentions_gene(c["work"], gene_terms)
                ]
                removed = before - len(scored)
                if removed:
                    logger.info(
                        f"Gene filter ({hop_label}): removed {removed}/{before} "
                        f"candidates (no gene mention in title/abstract)"
                    )

            # Re-sort after bib coupling update
            scored.sort(key=lambda x: x["effective_score"], reverse=True)

            # Track seen PMIDs to avoid duplicates across hops
            for c in scored:
                all_seen_pmids.add(c["pmid"])

            all_candidates.extend(scored)

        # Deduplicate across hops (keep highest score)
        seen: set[str] = set()
        deduped: list[dict] = []
        all_candidates.sort(key=lambda x: x["effective_score"], reverse=True)
        for c in all_candidates:
            if c["pmid"] not in seen:
                seen.add(c["pmid"])
                deduped.append(c)

        logger.info(
            f"Citation expansion complete: {len(deduped)} final candidates "
            f"across {min(max_hops, len(all_candidates) > 0 and max_hops or 1)} hop(s)"
        )

        return deduped

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
    def _mentions_gene(work: dict, gene_terms: list[str]) -> bool:
        """Check if a work mentions any gene term in title or abstract."""
        import re
        title = work.get("title", "") or ""
        abstract = _invert_abstract(work.get("abstract_inverted_index"))
        text = f"{title} {abstract}"
        for term in gene_terms:
            if re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _filter_by_text(works: list[dict], exclude_terms: list[str]) -> list[dict]:
        """Remove works whose title or abstract contains any exclude term."""
        import re
        patterns = [
            re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE)
            for t in exclude_terms
        ]
        kept = []
        for w in works:
            title = w.get("title", "") or ""
            abstract = _invert_abstract(w.get("abstract_inverted_index"))
            text = f"{title} {abstract}"
            if any(p.search(text) for p in patterns):
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
            "abstract": _invert_abstract(work.get("abstract_inverted_index")),
            "publication_type": [work.get("type", "")],
            "volume": "",
            "issue": "",
            "pages": "",
            "cited_by_count": work.get("cited_by_count", 0),
        }
