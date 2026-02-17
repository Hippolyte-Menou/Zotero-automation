"""OpenAlex API client for citation network expansion.

Uses the OpenAlex Academic Graph API (https://docs.openalex.org/) to discover
papers connected to seed publications via one-hop citation traversal.
"""

import time
import logging
from collections import defaultdict

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openalex.org"


class OpenAlexClient:
    """Client for the OpenAlex API (citation network expansion)."""

    def __init__(self, api_key: str | None = None, delay: float = 0.05):
        self.session = requests.Session()
        self.delay = delay
        self.params: dict[str, str] = {}
        if api_key:
            self.params["api_key"] = api_key

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

    def resolve_pmid(self, pmid: str) -> dict | None:
        """Look up a single work by PMID. Returns OpenAlex work object or None."""
        data = self._get(
            f"/works/pmid:{pmid}",
            params={"select": "id,ids,cited_by_count,referenced_works"},
        )
        return data

    def batch_resolve_pmids(self, pmids: list[str]) -> dict[str, dict]:
        """Resolve a list of PMIDs to OpenAlex work data.

        Uses the filter endpoint with pipe-separated PMIDs (OR logic)
        to batch-resolve efficiently.

        Returns {pmid: {openalex_id, cited_by_count, referenced_works}}.
        """
        result: dict[str, dict] = {}
        batch_size = 50  # keep URL length reasonable

        for i in range(0, len(pmids), batch_size):
            batch = pmids[i:i + batch_size]
            pmid_filter = "|".join(batch)
            page = 1
            while True:
                data = self._get(
                    "/works",
                    params={
                        "filter": f"pmid:{pmid_filter}",
                        "select": "id,ids,cited_by_count,referenced_works",
                        "per_page": "200",
                        "page": str(page),
                    },
                )
                if not data or "results" not in data:
                    break
                for work in data["results"]:
                    work_pmid = self._extract_pmid(work)
                    if work_pmid:
                        result[work_pmid] = work
                if len(data["results"]) < 200:
                    break
                page += 1

            logger.info(
                f"OpenAlex batch resolve: {len(batch)} PMIDs -> "
                f"{sum(1 for p in batch if p in result)} found"
            )

        return result

    def get_references(self, openalex_id: str) -> list[str]:
        """Get OpenAlex IDs of works referenced by the given work (backward).

        The referenced_works field is already in the work object, so this
        just returns those IDs. No extra API call needed if we already have
        the work object.
        """
        # This is a fallback for when we don't have referenced_works cached
        data = self._get(
            f"/works/{openalex_id}",
            params={"select": "referenced_works"},
        )
        if not data:
            return []
        return data.get("referenced_works", [])

    def get_citations(self, openalex_id: str, limit: int = 500) -> list[dict]:
        """Get works that cite the given work (forward citations).

        Returns list of work objects with PMIDs where available.
        """
        results = []
        page = 1
        per_page = min(limit, 200)

        while len(results) < limit:
            data = self._get(
                "/works",
                params={
                    "filter": f"cites:{openalex_id},has_pmid:true",
                    "select": "id,ids,cited_by_count",
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

    def _resolve_openalex_ids_to_pmids(self, openalex_ids: list[str]) -> dict[str, str]:
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

    def expand_seeds(
        self,
        seed_pmids: list[str],
        existing_pmids: set[str],
        max_seeds: int = 100,
    ) -> list[dict]:
        """Expand seed papers via one-hop citation network.

        Returns candidate papers sorted by co-citation count (how many
        seed papers link to each candidate).
        """
        if not seed_pmids:
            return []

        # 1. Resolve PMIDs to OpenAlex works
        logger.info(f"Citation expansion: resolving {len(seed_pmids)} seed PMIDs")
        paper_map = self.batch_resolve_pmids(seed_pmids)
        logger.info(f"Resolved {len(paper_map)}/{len(seed_pmids)} PMIDs in OpenAlex")

        if not paper_map:
            return []

        # 2. If too many seeds, keep the most-cited ones
        seeds = list(paper_map.values())
        if len(seeds) > max_seeds:
            seeds.sort(key=lambda p: p.get("cited_by_count", 0), reverse=True)
            seeds = seeds[:max_seeds]
            logger.info(f"Trimmed to top {max_seeds} most-cited seed papers")

        seed_pmid_set = set(seed_pmids)

        # 3. Collect candidate OpenAlex IDs from references + forward citations
        # candidate_id -> {co_citations, directions}
        candidates: dict[str, dict] = defaultdict(
            lambda: {"co_citations": 0, "directions": set()}
        )

        for i, work in enumerate(seeds):
            openalex_id = work.get("id", "")
            if (i + 1) % 20 == 0:
                logger.info(f"Expanding seed {i + 1}/{len(seeds)}")

            # Backward: referenced_works is already in the work object
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

        logger.info(f"Raw candidates from citation network: {len(candidates)}")

        # 4. Resolve candidate OpenAlex IDs to PMIDs (only those with PMIDs)
        candidate_ids = list(candidates.keys())
        id_to_pmid = self._resolve_openalex_ids_to_pmids(candidate_ids)

        logger.info(f"Candidates with PMIDs: {len(id_to_pmid)}")

        # 5. Build result, filtering out existing and seed PMIDs
        result = []
        for oa_id, pmid in id_to_pmid.items():
            if pmid in existing_pmids or pmid in seed_pmid_set:
                continue

            info = candidates[oa_id]
            dirs = info["directions"]
            if "reference" in dirs and "citation" in dirs:
                direction = "both"
            elif "citation" in dirs:
                direction = "citation"
            else:
                direction = "reference"

            result.append({
                "pmid": pmid,
                "co_citations": info["co_citations"],
                "direction": direction,
            })

        result.sort(key=lambda x: x["co_citations"], reverse=True)

        logger.info(
            f"Citation expansion: {len(seeds)} seeds -> {len(result)} unique candidates "
            f"(excluded {len(existing_pmids)} existing + {len(seed_pmid_set)} seeds)"
        )

        return result

    @staticmethod
    def _extract_pmid(work: dict) -> str | None:
        ids = work.get("ids", {})
        pmid = ids.get("pmid", "")
        if pmid:
            # OpenAlex returns "https://pubmed.ncbi.nlm.nih.gov/12345678"
            return pmid.rstrip("/").split("/")[-1]
        return None
