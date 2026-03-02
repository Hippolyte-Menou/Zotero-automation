"""OpenAlex API client for gene and topic literature discovery.

Uses the OpenAlex Academic Graph API (https://docs.openalex.org/) for both
keyword-based paper search and citation network expansion.

Supports two search modes:
- Gene search: (gene_name OR aliases) AND (disease_keywords)
- Topic search: keyword-based with OpenAlex topic/subfield scoping

Adaptive selectivity: the more papers already exist in the library for a gene
or topic, the higher the bar (co-citation count or recency) a new candidate
must clear.
"""

import re
import time
import logging
import math
import datetime
from collections import defaultdict

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openalex.org"

# Fields fetched for every work in search results and citation expansion.
# Enough to build a Zotero item without a second API call.
WORK_FIELDS = (
    "id,ids,title,publication_year,publication_date,cited_by_count,"
    "type,primary_location,authorships,referenced_works,"
    "abstract_inverted_index,biblio,mesh,language"
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
        self._failed_requests = 0

    @property
    def failed_request_count(self) -> int:
        return self._failed_requests

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, params: dict | None = None) -> dict | None:
        url = f"{BASE_URL}{endpoint}"
        merged = {**self.params, **(params or {})}
        attempt = 0
        rate_limit_waits = 0
        while attempt < 3:
            try:
                resp = self.session.get(url, params=merged, timeout=15)
                if resp.status_code == 429:
                    rate_limit_waits += 1
                    if rate_limit_waits > 5:
                        logger.error("Rate limited too many times, giving up")
                        self._failed_requests += 1
                        return None
                    wait = 2 ** rate_limit_waits
                    logger.warning(f"Rate limited (429), waiting {wait}s (retry {rate_limit_waits}/5)")
                    time.sleep(wait)
                    continue  # Do NOT increment attempt
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                remaining = resp.headers.get("X-RateLimit-Remaining-USD")
                if remaining is not None:
                    logger.debug(f"OpenAlex budget remaining: ${remaining}")
                time.sleep(self.delay)
                return resp.json()
            except requests.exceptions.RequestException as e:
                attempt += 1
                if attempt < 3:
                    wait = 2 ** attempt
                    logger.warning(f"Request failed ({e}), retrying in {wait}s")
                    time.sleep(wait)
                else:
                    logger.error(f"Request failed after 3 attempts: {e}")
                    self._failed_requests += 1
                    return None
        self._failed_requests += 1
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
        rejection_log=None,
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
            "is_retracted:false",
            "language:en|fr",
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
            results = self._filter_by_text(
                results, exclude_terms, rejection_log=rejection_log
            )
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
            "is_retracted:false",
            "language:en|fr",
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
    # Topic search
    # ------------------------------------------------------------------

    def _search_topic_query(
        self,
        query: str,
        max_results: int,
        type_filter: str | None = None,
        subfield_id: int | None = None,
        topic_ids: list[str] | None = None,
        extra_filters: list[str] | None = None,
    ) -> list[dict]:
        """Execute a single OpenAlex topic query with pagination.

        Builds the filter string from:
        - title_and_abstract.search:{query}
        - primary_topic.subfield.id:{subfield_id} (optional)
        - topics.id:{id1|id2|...} (optional)
        - type:{type_filter} (optional, e.g. "review|article")
        - has_pmid, is_retracted, language (standard)
        - any extra_filters (e.g. from_publication_date)
        """
        filters = [
            f"title_and_abstract.search:{query}",
            "has_pmid:true",
            "is_retracted:false",
            "language:en|fr",
        ]
        if type_filter:
            filters.append(f"type:{type_filter}")
        if subfield_id:
            filters.append(f"primary_topic.subfield.id:{subfield_id}")
        if topic_ids:
            filters.append(f"topics.id:{'|'.join(str(t) for t in topic_ids)}")
        if extra_filters:
            filters.extend(extra_filters)
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
            if total > max_results and page == 1:
                logger.info(
                    f"Topic query returned {total} results, capping at {max_results}"
                )
            if len(batch) < per_page:
                break
            page += 1

        return results[:max_results]

    def search_topic(
        self,
        keywords: list[str],
        exclude_terms: list[str] | None = None,
        exclude_mesh: list[str] | None = None,
        max_results: int = 50,
        type_filter: str | None = None,
        subfield_id: int | None = None,
        topic_ids: list[str] | None = None,
        clinical_keywords: list[str] | None = None,
        rejection_log=None,
    ) -> list[dict]:
        """Search OpenAlex for works matching topic keywords.

        Two modes:
        - Keyword mode (no clinical_keywords): OR all keywords into one query.
          Used for anatomy, embryology, physiology, examinations.
        - Disease+clinical mode (with clinical_keywords): for each keyword,
          AND with clinical terms, merge results. Used for pathologies.

        Multi-word keywords are auto-quoted for phrase matching.

        Returns list of work dicts with full metadata.
        """
        if not keywords:
            return []

        def _quote(kw: str) -> str:
            return f'"{kw}"' if " " in kw else kw

        if clinical_keywords:
            # Disease+clinical mode: iterate per disease keyword
            clinical_part = " OR ".join(_quote(ck) for ck in clinical_keywords)
            per_disease_cap = max(5, max_results // max(len(keywords), 1))
            all_results: list[dict] = []
            seen_ids: set[str] = set()

            for kw in keywords:
                query = f"({_quote(kw)}) AND ({clinical_part})"
                logger.info(f"Topic search (disease+clinical): {query}")
                batch = self._search_topic_query(
                    query=query,
                    max_results=per_disease_cap,
                    type_filter=type_filter,
                    subfield_id=subfield_id,
                    topic_ids=topic_ids,
                )
                for w in batch:
                    oa_id = w.get("id", "")
                    if oa_id not in seen_ids:
                        seen_ids.add(oa_id)
                        all_results.append(w)

            logger.info(
                f"Topic search (disease+clinical): "
                f"{len(all_results)} unique works from {len(keywords)} queries"
            )
            results = all_results[:max_results]
        else:
            # Keyword mode: OR all keywords
            query = " OR ".join(_quote(kw) for kw in keywords)
            logger.info(f"Topic search (keyword): {query}")
            results = self._search_topic_query(
                query=query,
                max_results=max_results,
                type_filter=type_filter,
                subfield_id=subfield_id,
                topic_ids=topic_ids,
            )
            logger.info(f"Topic search: {len(results)} works found")

        # Post-hoc text exclusion
        if exclude_terms:
            before = len(results)
            results = self._filter_by_text(
                results, exclude_terms, rejection_log=rejection_log
            )
            if before - len(results):
                logger.info(f"Text filter removed {before - len(results)} works")

        # Post-hoc MeSH exclusion
        if exclude_mesh:
            results = self.filter_by_mesh(
                results, exclude_mesh, rejection_log=rejection_log
            )

        return results

    def search_topic_recent(
        self,
        keywords: list[str],
        max_results: int = 5,
        type_filter: str | None = None,
        subfield_id: int | None = None,
        topic_ids: list[str] | None = None,
        year: int | None = None,
    ) -> list[dict]:
        """Search OpenAlex for recent topic papers (current year by default).

        Keyword-only mode (OR all keywords), filtered to papers published
        from year-01-01 onwards. No clinical_keywords for the recent pass.

        Returns list of work dicts with full metadata.
        """
        if year is None:
            year = datetime.date.today().year

        if not keywords:
            return []

        def _quote(kw: str) -> str:
            return f'"{kw}"' if " " in kw else kw

        query = " OR ".join(_quote(kw) for kw in keywords)
        date_filter = f"{year}-01-01"
        logger.info(f"Topic recent-papers query (from {date_filter}): {query}")

        results = self._search_topic_query(
            query=query,
            max_results=max_results,
            type_filter=type_filter,
            subfield_id=subfield_id,
            topic_ids=topic_ids,
            extra_filters=[f"from_publication_date:{date_filter}"],
        )
        logger.info(f"Topic recent-papers search: {len(results)} works found")
        return results

    # ------------------------------------------------------------------
    # Citation network expansion
    # ------------------------------------------------------------------

    def get_citations(
        self,
        openalex_id: str,
        limit: int = 500,
        since_date: str | None = None,
    ) -> list[dict]:
        """Get works that cite the given work (forward citations).

        If since_date is provided (YYYY-MM-DD), only returns citations
        published on or after that date.
        """
        results = []
        page = 1
        per_page = min(limit, 200)

        filter_parts = [
            f"cites:{openalex_id}",
            "has_pmid:true",
            "is_retracted:false",
            "language:en|fr",
        ]
        if since_date:
            filter_parts.append(f"from_publication_date:{since_date}")
        filter_str = ",".join(filter_parts)

        while len(results) < limit:
            data = self._get(
                "/works",
                params={
                    "filter": filter_str,
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

    def _should_expand_seed(self, work: dict, citation_cache: dict | None) -> bool:
        """Check if a seed needs forward citation expansion.

        Returns False (skip) if the seed's cited_by_count matches the cached
        value, meaning no new citations since last run.
        """
        if citation_cache is None:
            return True
        oa_id = work.get("id", "")
        if not oa_id:
            return True
        cached = citation_cache.get("seeds", {}).get(oa_id)
        if cached is None:
            return True
        current_count = work.get("cited_by_count", 0)
        cached_count = cached.get("cited_by_count", -1)
        return current_count != cached_count

    def _expand_one_hop(
        self,
        seed_works: list[dict],
        existing_pmids: set[str],
        library_size: int,
        max_seeds: int = 100,
        min_co_citations: int = 1,
        max_min_co: int = 6,
        hop_label: str = "hop 1",
        below_threshold: list[dict] | None = None,
        citation_cache: dict | None = None,
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

        seed_pmids = {self.extract_pmid(w) for w in seed_works}

        # Build seed reference set for bibliographic coupling
        seed_reference_set: set[str] = set()
        for w in seeds:
            refs = w.get("referenced_works", [])
            if not refs and citation_cache is not None:
                oa_id = w.get("id", "")
                refs = citation_cache.get("seeds", {}).get(oa_id, {}).get("referenced_works", [])
            seed_reference_set.update(refs)

        # candidate OpenAlex ID -> {co_citations, bib_coupling, directions, year, referenced_works}
        candidates: dict[str, dict] = defaultdict(
            lambda: {"co_citations": 0, "bib_coupling": 0, "directions": set(), "year": 0}
        )

        # Derive since_date for incremental forward citations (7-day buffer)
        since_date: str | None = None
        if citation_cache is not None:
            last_run = citation_cache.get("last_run_date")
            if last_run:
                buffer_date = (
                    datetime.date.fromisoformat(last_run)
                    - datetime.timedelta(days=7)
                )
                since_date = buffer_date.isoformat()

        skipped_seeds = 0
        for i, work in enumerate(seeds):
            openalex_id = work.get("id", "")
            if (i + 1) % 20 == 0 or (i + 1) == len(seeds):
                logger.info(f"Expanding seed {i + 1}/{len(seeds)} ({hop_label})")

            # Backward: use work object's referenced_works, fall back to cache
            refs = work.get("referenced_works", [])
            if not refs and citation_cache is not None:
                refs = citation_cache.get("seeds", {}).get(openalex_id, {}).get("referenced_works", [])
            for ref_id in refs:
                candidates[ref_id]["co_citations"] += 1
                candidates[ref_id]["directions"].add("reference")

            # Forward: query works that cite this seed
            if self._should_expand_seed(work, citation_cache):
                citing_works = self.get_citations(
                    openalex_id, limit=500, since_date=since_date
                )
                # Update cache entry (including referenced_works)
                if citation_cache is not None:
                    entry = citation_cache.setdefault("seeds", {}).setdefault(openalex_id, {})
                    entry["cited_by_count"] = work.get("cited_by_count", 0)
                    entry["last_expanded"] = datetime.date.today().isoformat()
                    entry["referenced_works"] = work.get("referenced_works", [])
            else:
                citing_works = []
                skipped_seeds += 1
                # Cache referenced_works for skipped seeds too
                if citation_cache is not None and openalex_id:
                    entry = citation_cache.setdefault("seeds", {}).setdefault(openalex_id, {})
                    if "referenced_works" not in entry:
                        entry["referenced_works"] = work.get("referenced_works", [])

            for citing in citing_works:
                cit_id = citing.get("id", "")
                if cit_id:
                    candidates[cit_id]["co_citations"] += 1
                    candidates[cit_id]["directions"].add("citation")
                    year = citing.get("publication_year", 0) or 0
                    if year > candidates[cit_id]["year"]:
                        candidates[cit_id]["year"] = year

        logger.info(
            f"Citation expansion ({hop_label}): skipped {skipped_seeds}/{len(seeds)} "
            f"seeds (cited_by_count unchanged)"
        )
        logger.info(f"Raw candidates from citation network ({hop_label}): {len(candidates)}")

        # Resolve OpenAlex IDs to PMIDs (only those with PMIDs)
        candidate_ids = [
            oa_id for oa_id, info in candidates.items()
            if info["co_citations"] >= 1
        ]
        logger.info(f"Candidates with co_citations >= 1 ({hop_label}): {len(candidate_ids)} (filtered from {len(candidates)})")
        id_to_pmid = self.resolve_openalex_ids_to_pmids(candidate_ids)
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
                if below_threshold is not None:
                    dirs_bt = info["directions"]
                    if "reference" in dirs_bt and "citation" in dirs_bt:
                        dir_bt = "both"
                    elif "citation" in dirs_bt:
                        dir_bt = "citation"
                    else:
                        dir_bt = "reference"
                    below_threshold.append({
                        "pmid": pmid,
                        "openalex_id": oa_id,
                        "co_citations": co_cit,
                        "bib_coupling": bib_coupling,
                        "recency_bonus": recency_bonus,
                        "effective_score": effective_co,
                        "threshold": adaptive_min_co,
                        "year": year,
                        "direction": dir_bt,
                    })
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
        mention_terms: list[str] | None = None,
        max_hops: int = 1,
        hop2_top_n: int = 10,
        exclude_mesh: list[str] | None = None,
        rejection_log=None,
        citation_cache: dict | None = None,
    ) -> list[dict]:
        """Multi-hop citation expansion with mention filtering and
        bibliographic coupling.

        Hop 1: expand seed_works via citation network.
        Hop 2 (if max_hops >= 2): top candidates from hop 1 become new seeds.

        After each hop, candidates are filtered to only keep papers that
        mention the search terms (gene names, disease names, topic keywords)
        in their title or abstract, then further filtered by MeSH exclusion
        if exclude_mesh is provided.

        Returns final scored candidate list with full work metadata attached
        (key 'work' on each candidate dict).
        """
        if not seed_works:
            return []

        terms = mention_terms
        mention_patterns = self.compile_mention_patterns(terms) if terms else []

        all_seen_pmids = set(existing_pmids)
        all_candidates: list[dict] = []
        pre_mention_total = 0
        mention_removed_total = 0

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

            hop_below: list[dict] = [] if rejection_log else []
            scored = self._expand_one_hop(
                seed_works=hop_seeds,
                existing_pmids=all_seen_pmids,
                library_size=library_size,
                max_seeds=max_seeds,
                min_co_citations=min_co_citations,
                max_min_co=max_min_co,
                hop_label=hop_label,
                below_threshold=hop_below if rejection_log else None,
                citation_cache=citation_cache,
            )

            # Log below-threshold candidates (top 50 by score)
            if rejection_log and hop_below:
                hop_below.sort(
                    key=lambda x: x["effective_score"], reverse=True
                )
                top_below = hop_below[:50]
                below_pmids = [b["pmid"] for b in top_below if b.get("pmid")]
                if below_pmids:
                    below_works = self.fetch_works_by_pmids(below_pmids)
                    below_by_pmid = {
                        self.extract_pmid(w): w for w in below_works
                    }
                    for b in top_below:
                        work = below_by_pmid.get(b["pmid"])
                        if work:
                            rejection_log.add_from_work(
                                work,
                                reason="score_below_threshold",
                                scores=b,
                            )
                    logger.info(
                        f"Rejection log ({hop_label}): recorded "
                        f"{len(top_below)} below-threshold candidates"
                    )

            if not scored:
                logger.info(f"No candidates from {hop_label}")
                break

            # Fetch full metadata for candidates
            candidate_pmids = [c["pmid"] for c in scored]
            candidate_works = self.fetch_works_by_pmids(candidate_pmids)
            work_by_pmid = {self.extract_pmid(w): w for w in candidate_works}

            # Compute bibliographic coupling from full metadata
            # Build seed reference set (with cache fallback)
            seed_ref_set: set[str] = set()
            for w in hop_seeds:
                refs = w.get("referenced_works", [])
                if not refs and citation_cache is not None:
                    oa_id = w.get("id", "")
                    refs = citation_cache.get("seeds", {}).get(oa_id, {}).get("referenced_works", [])
                seed_ref_set.update(refs)

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

            # Mention filter (gene names, disease names, topic keywords)
            if terms:
                before = len(scored)
                pre_mention_total += before
                kept_mention = []
                for c in scored:
                    if self.mentions_terms(c["work"], mention_patterns):
                        kept_mention.append(c)
                    elif rejection_log:
                        rejection_log.add_from_work(
                            c["work"],
                            reason="mention_filter",
                            scores={
                                "co_citations": c.get("co_citations"),
                                "bib_coupling": c.get("bib_coupling"),
                                "recency_bonus": c.get("recency_bonus"),
                                "effective_score": c.get("effective_score"),
                                "direction": c.get("direction"),
                            },
                        )
                scored = kept_mention
                removed = before - len(scored)
                mention_removed_total += removed
                if removed:
                    logger.info(
                        f"Mention filter ({hop_label}): removed {removed}/{before} "
                        f"candidates (no term mention in title/abstract)"
                    )

            # MeSH exclusion filter
            if exclude_mesh:
                before = len(scored)
                hop_works = [c["work"] for c in scored]
                kept_works = self.filter_by_mesh(
                    hop_works, exclude_mesh, rejection_log=rejection_log
                )
                kept_pmids = {self.extract_pmid(w) for w in kept_works}
                scored = [c for c in scored if c["pmid"] in kept_pmids]
                if len(scored) < before:
                    logger.info(
                        f"MeSH exclusion ({hop_label}): removed "
                        f"{before - len(scored)} candidates"
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

        if not deduped and pre_mention_total > 0 and mention_removed_total == pre_mention_total:
            logger.warning(
                f"Mention filter removed ALL {pre_mention_total} citation "
                f"candidate(s) -- search terms may be too narrow or "
                f"candidates lack relevant title/abstract text. "
                f"Terms used: {terms}"
            )

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
                        "filter": f"pmid:{pmid_filter},is_retracted:false",
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

    def fetch_works_by_dois(self, dois: list[str]) -> list[dict]:
        """Fetch work metadata for a list of DOIs.

        Returns list of work dicts (same shape as fetch_works_by_pmids).
        DOIs are normalised to lowercase bare form before querying.
        Uses batches of up to 50 per request.
        """
        result: list[dict] = []
        batch_size = 50

        for i in range(0, len(dois), batch_size):
            batch = dois[i : i + batch_size]
            normalized_batch = [
                d.replace("https://doi.org/", "").replace("http://doi.org/", "").lower()
                for d in batch
            ]
            doi_filter = "|".join(normalized_batch)
            page = 1
            while True:
                data = self._get(
                    "/works",
                    params={
                        "filter": f"doi:{doi_filter},is_retracted:false",
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

        logger.info(f"Fetched {len(result)} works by DOI from OpenAlex")
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def resolve_openalex_ids_to_pmids(
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
                        "filter": f"ids.openalex:{id_filter},has_pmid:true,is_retracted:false",
                        "select": "id,ids",
                        "per_page": "200",
                        "page": str(page),
                    },
                )
                if not data or "results" not in data:
                    break
                for work in data["results"]:
                    pmid = self.extract_pmid(work)
                    oa_id = work.get("id", "")
                    if pmid and oa_id:
                        result[oa_id] = pmid
                if len(data["results"]) < 200:
                    break
                page += 1

        return result

    @staticmethod
    def compile_mention_patterns(terms: list[str]) -> list[re.Pattern]:
        """Pre-compile word-boundary regex patterns for mention filtering."""
        return [re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE) for t in terms]

    @staticmethod
    def mentions_terms(work: dict, patterns: list[re.Pattern]) -> bool:
        """Check if a work mentions any term using pre-compiled patterns."""
        title = work.get("title", "") or ""
        abstract = _invert_abstract(work.get("abstract_inverted_index"))
        text = f"{title} {abstract}"
        for pat in patterns:
            if pat.search(text):
                return True
        return False

    @staticmethod
    def filter_by_mesh(
        works: list[dict],
        exclude_mesh: list[str],
        rejection_log=None,
    ) -> list[dict]:
        """Remove works whose MeSH descriptors contain any excluded descriptor name.

        Works with an empty mesh array are always kept -- not all PubMed records
        have MeSH terms assigned (recent publications, non-indexed journals).
        Only works that have >= 1 MeSH entry AND a match in exclude_mesh are dropped.

        exclude_mesh: list of MeSH descriptor names, matched case-insensitively.
                      e.g. ["Neoplasms", "Diabetes Mellitus"]

        If rejection_log is provided, rejected works are recorded with the
        first matched MeSH descriptor.
        """
        if not exclude_mesh:
            return works

        blocked = {term.lower() for term in exclude_mesh}
        kept = []
        removed = 0
        for w in works:
            mesh_entries = w.get("mesh") or []
            if not mesh_entries:
                kept.append(w)
                continue
            descriptors = {
                (e.get("descriptor_name") or "").lower()
                for e in mesh_entries
            }
            matched = descriptors & blocked
            if matched:
                removed += 1
                if rejection_log:
                    # Report the first matching MeSH term (original casing)
                    matched_term = next(iter(matched))
                    rejection_log.add_from_work(
                        w, reason="mesh_exclusion", matched_term=matched_term
                    )
            else:
                kept.append(w)
        if removed:
            logger.info(f"MeSH exclusion filter removed {removed} works")
        return kept

    @staticmethod
    def _filter_by_text(
        works: list[dict],
        exclude_terms: list[str],
        rejection_log=None,
    ) -> list[dict]:
        """Remove works whose title or abstract contains any exclude term.

        If rejection_log is provided, rejected works are recorded with the
        matched term.
        """
        patterns = [
            (re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE), t)
            for t in exclude_terms
        ]
        kept = []
        for w in works:
            title = w.get("title", "") or ""
            abstract = _invert_abstract(w.get("abstract_inverted_index"))
            text = f"{title} {abstract}"
            matched = None
            for pat, term in patterns:
                if pat.search(text):
                    matched = term
                    break
            if matched:
                if rejection_log:
                    rejection_log.add_from_work(
                        w, reason="text_exclusion", matched_term=matched
                    )
                continue
            kept.append(w)
        return kept

    @staticmethod
    def extract_pmid(work: dict) -> str | None:
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
        journal_abbr = source.get("abbreviated_title", "")

        # Volume / issue / pages from biblio
        biblio = work.get("biblio") or {}
        volume = biblio.get("volume") or ""
        issue = biblio.get("issue") or ""
        first_page = biblio.get("first_page") or ""
        last_page = biblio.get("last_page") or ""
        pages = f"{first_page}-{last_page}" if first_page and last_page else first_page

        # Authors -- preserve as list of (first_name, last_name) tuples
        # so zotero_client can assign the correct Zotero creator fields.
        # OpenAlex display_name is "First [Middle] Last" but raw_author_name
        # can be "Last, First" for non-Western records; we prefer the raw form
        # when it contains a comma because it gives an unambiguous split.
        authors = []
        for authorship in work.get("authorships", []):
            author = authorship.get("author", {})
            display = author.get("display_name", "") or ""
            raw = authorship.get("raw_author_name", "") or ""

            if "," in raw:
                # "Zaghloul, Khaled K." -> last="Zaghloul", first="Khaled K."
                last, _, first = raw.partition(",")
                authors.append((first.strip(), last.strip()))
            elif display:
                # "First [Middle] Last" -> rsplit on last space
                parts = display.rsplit(" ", 1)
                if len(parts) == 2:
                    authors.append((parts[0], parts[1]))
                else:
                    authors.append(("", display))

        # MeSH descriptor names (PubMed works only; empty list for others)
        mesh_entries = work.get("mesh") or []
        mesh_terms = [
            e["descriptor_name"]
            for e in mesh_entries
            if e.get("descriptor_name")
        ]

        return {
            "pmid": pmid,
            "doi": doi,
            "title": work.get("title", ""),
            "authors": authors,
            "journal": journal,
            "journal_abbr": journal_abbr,
            "year": str(work.get("publication_year", "")),
            "date_published": work.get("publication_date", ""),
            "abstract": _invert_abstract(work.get("abstract_inverted_index")),
            "publication_type": [work.get("type", "")],
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "cited_by_count": work.get("cited_by_count", 0),
            "mesh_terms": mesh_terms,
        }
