"""Exhaustive PubMed search using Entrez."""

import time
import logging
from Bio import Entrez, Medline

logger = logging.getLogger(__name__)


def build_query(
    gene_symbols: set[str],
    mesh_exclusions: list[str] | None = None,
    disease_terms: str | None = None,
) -> str:
    """
    Build a PubMed query string.
    Gene symbols are OR'd together (quoted, all fields).
    Disease terms (if provided) are AND'd to scope results.
    MeSH exclusions applied as NOT "Term"[MeSH].
    """
    gene_terms = " OR ".join(f'"{s}"' for s in sorted(gene_symbols))
    query = f"({gene_terms})"

    if disease_terms:
        query += f" AND {disease_terms}"

    if mesh_exclusions:
        for term in mesh_exclusions:
            query += f' NOT "{term}"[MeSH]'

    return query


def search_pubmed(
    query: str,
    email: str,
    api_key: str | None = None,
    max_results: int = 10000,
    delay: float = 0.34,
) -> list[dict]:
    """
    Run a PubMed query and return all matching records as dicts.
    Uses Entrez history server for efficient batch retrieval.
    """
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key

    logger.info(f"PubMed query: {query}")

    # Search to get count + history key
    handle = Entrez.esearch(db="pubmed", term=query, retmax=0, usehistory="y")
    search_results = Entrez.read(handle)
    handle.close()

    total = int(search_results["Count"])
    query_key = search_results["QueryKey"]
    web_env = search_results["WebEnv"]

    logger.info(f"Found {total} results")

    if total == 0:
        return []

    if total > max_results:
        logger.warning(
            f"Query returned {total} results, capping at {max_results}. "
            f"Consider narrowing your exclusions or checking aliases."
        )
        total = max_results

    # Fetch in batches via history server
    records = []
    batch_size = 200

    for start in range(0, total, batch_size):
        logger.info(f"Fetching records {start + 1}-{min(start + batch_size, total)} / {total}")
        handle = Entrez.efetch(
            db="pubmed",
            rettype="medline",
            retmode="text",
            query_key=query_key,
            WebEnv=web_env,
            retstart=start,
            retmax=batch_size,
        )
        batch = list(Medline.parse(handle))
        handle.close()
        records.extend(batch)
        time.sleep(delay)

    # Parse into clean dicts
    parsed = []
    for r in records:
        pmid = r.get("PMID", "")

        # Extract DOI
        doi = ""
        lid = r.get("LID", "")
        if isinstance(lid, list):
            for entry in lid:
                if "[doi]" in entry:
                    doi = entry.replace(" [doi]", "").strip()
                    break
        elif isinstance(lid, str) and "[doi]" in lid:
            doi = lid.replace(" [doi]", "").strip()

        if not doi:
            aid = r.get("AID", [])
            if isinstance(aid, str):
                aid = [aid]
            for entry in aid:
                if "[doi]" in entry:
                    doi = entry.replace(" [doi]", "").strip()
                    break

        parsed.append({
            "pmid": pmid,
            "doi": doi,
            "title": r.get("TI", ""),
            "authors": r.get("AU", []),
            "journal": r.get("JT", ""),
            "journal_abbr": r.get("TA", ""),
            "year": r.get("DP", "")[:4],
            "date_published": r.get("DP", ""),
            "abstract": r.get("AB", ""),
            "mesh_terms": r.get("MH", []),
            "publication_type": r.get("PT", []),
            "volume": r.get("VI", ""),
            "issue": r.get("IP", ""),
            "pages": r.get("PG", ""),
        })

    logger.info(f"Parsed {len(parsed)} records")
    return parsed


def fetch_by_pmids(
    pmids: list[str],
    email: str,
    api_key: str | None = None,
    delay: float = 0.34,
) -> list[dict]:
    """Fetch full Medline records for a list of known PMIDs.

    Unlike search_pubmed(), this skips esearch and uses efetch directly.
    Returns the same dict format as search_pubmed().
    """
    if not pmids:
        return []

    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key

    logger.info(f"Fetching {len(pmids)} records by PMID")

    records = []
    batch_size = 200

    for start in range(0, len(pmids), batch_size):
        batch = pmids[start:start + batch_size]
        logger.info(f"Fetching PMIDs {start + 1}-{start + len(batch)} / {len(pmids)}")
        handle = Entrez.efetch(
            db="pubmed",
            id=",".join(batch),
            rettype="medline",
            retmode="text",
        )
        parsed_batch = list(Medline.parse(handle))
        handle.close()
        records.extend(parsed_batch)
        time.sleep(delay)

    # Parse into clean dicts (same logic as search_pubmed)
    parsed = []
    for r in records:
        pmid = r.get("PMID", "")

        doi = ""
        lid = r.get("LID", "")
        if isinstance(lid, list):
            for entry in lid:
                if "[doi]" in entry:
                    doi = entry.replace(" [doi]", "").strip()
                    break
        elif isinstance(lid, str) and "[doi]" in lid:
            doi = lid.replace(" [doi]", "").strip()

        if not doi:
            aid = r.get("AID", [])
            if isinstance(aid, str):
                aid = [aid]
            for entry in aid:
                if "[doi]" in entry:
                    doi = entry.replace(" [doi]", "").strip()
                    break

        parsed.append({
            "pmid": pmid,
            "doi": doi,
            "title": r.get("TI", ""),
            "authors": r.get("AU", []),
            "journal": r.get("JT", ""),
            "journal_abbr": r.get("TA", ""),
            "year": r.get("DP", "")[:4],
            "date_published": r.get("DP", ""),
            "abstract": r.get("AB", ""),
            "mesh_terms": r.get("MH", []),
            "publication_type": r.get("PT", []),
            "volume": r.get("VI", ""),
            "issue": r.get("IP", ""),
            "pages": r.get("PG", ""),
        })

    logger.info(f"Fetched {len(parsed)} records by PMID")
    return parsed
