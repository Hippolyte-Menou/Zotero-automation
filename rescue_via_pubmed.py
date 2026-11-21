#!/usr/bin/env python3
"""Fallback apply script using PubMed Entrez API instead of OpenAlex.

Runs the full audit_bot --apply flow but substitutes the OpenAlex rescue path
with PubMed efetch (batch of up to 200 PMIDs per HTTP call, no rate-limit
problems). Used when OpenAlex is persistently rate-limited.

Usage:
    python rescue_via_pubmed.py [--dry-run] [--work-dir WORK_DIR]
"""
import argparse
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import audit_bot
import run
from genebot import runtime

logger = logging.getLogger(__name__)

ENTREZ_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
# NCBI polite-use email (required for higher rate limits; identifies the caller)
NCBI_TOOL = "zotero-audit-bot"
NCBI_EMAIL = "hippolytefrmenou@gmail.com"


def _fetch_pubmed_xml(pmids: list[str], retries: int = 3) -> bytes:
    """Single efetch call for up to 200 PMIDs, returns raw XML bytes."""
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
        "tool": NCBI_TOOL,
        "email": NCBI_EMAIL,
    }
    for attempt in range(retries):
        try:
            resp = requests.get(ENTREZ_EFETCH, params=params, timeout=60)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt < retries - 1:
                wait = 2 ** attempt
                logger.warning("PubMed efetch attempt %d failed: %s — retrying in %ds", attempt + 1, exc, wait)
                time.sleep(wait)
            else:
                raise


def _parse_pubmed_xml(xml_bytes: bytes) -> dict[str, dict]:
    """Parse PubMed efetch XML into a {pmid: record} dict."""
    root = ET.fromstring(xml_bytes)
    records = {}
    for article in root.findall(".//PubmedArticle"):
        rec = _parse_article(article)
        if rec.get("pmid"):
            records[rec["pmid"]] = rec
    return records


def _parse_article(article: ET.Element) -> dict:
    mc = article.find("MedlineCitation")
    if mc is None:
        return {}
    pmid = mc.findtext("PMID", "")
    art = mc.find("Article")
    if art is None:
        return {"pmid": pmid}

    title_el = art.find("ArticleTitle")
    title = "".join(title_el.itertext()) if title_el is not None else ""

    authors = []
    for au in art.findall(".//AuthorList/Author"):
        last = au.findtext("LastName", "")
        first = au.findtext("ForeName", "") or au.findtext("Initials", "")
        if last:
            authors.append((first.strip(), last.strip()))
        else:
            collective = au.findtext("CollectiveName", "")
            if collective:
                authors.append(("", collective))

    journal = art.findtext(".//Journal/Title", "")
    journal_abbr = art.findtext(".//Journal/ISOAbbreviation", "")
    volume = art.findtext(".//JournalIssue/Volume", "")
    issue = art.findtext(".//JournalIssue/Issue", "")
    year = (
        art.findtext(".//JournalIssue/PubDate/Year")
        or art.findtext(".//JournalIssue/PubDate/MedlineDate", "")[:4]
    )
    pages = art.findtext(".//Pagination/MedlinePgn", "")

    abstract_parts = []
    for ab in art.findall(".//Abstract/AbstractText"):
        label = ab.get("Label")
        text = "".join(ab.itertext())
        abstract_parts.append(f"{label}: {text}" if label else text)
    abstract = " ".join(abstract_parts)

    doi = ""
    for id_el in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if id_el.get("IdType") == "doi":
            doi = id_el.text or ""
            break

    return {
        "pmid": pmid,
        "doi": doi,
        "title": title,
        "authors": authors,
        "journal": journal,
        "journal_abbr": journal_abbr,
        "year": year,
        "date_published": "",
        "abstract": abstract,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "type": "journalArticle",
        "mesh": [],
    }


def fetch_pubmed_records(pmids: list[str]) -> dict[str, dict]:
    """Batch-fetch PubMed records for all PMIDs (200 per request)."""
    records: dict[str, dict] = {}
    for i in range(0, len(pmids), 200):
        batch = pmids[i:i + 200]
        logger.info("Fetching PubMed batch %d-%d of %d", i + 1, min(i + 200, len(pmids)), len(pmids))
        xml_bytes = _fetch_pubmed_xml(batch)
        records.update(_parse_pubmed_xml(xml_bytes))
        if i + 200 < len(pmids):
            time.sleep(0.5)  # polite 500ms gap between batch requests
    return records


def make_pubmed_rescue_fn(zot, existing_pmids, existing_dois, pmid_to_key, genes_parent_key, pubmed_cache):
    """Return a rescue_fn that uses pre-fetched PubMed records instead of OpenAlex."""

    def rescue_fn(entries: list[dict]) -> tuple[int, list[dict]]:
        uploaded = 0
        failed: list[dict] = []

        for entry in entries:
            pmid = entry.get("pmid", "").strip()
            doi = entry.get("doi", "").strip()
            subcollection = entry.get("subcollection", "").strip()
            category = entry.get("category", "").strip()
            title = entry.get("title", "")

            if not pmid and not doi:
                logger.warning("Rescue: no PMID or DOI, skipping: %s", title)
                continue
            if pmid and pmid in existing_pmids:
                logger.info("Rescue: %s already in library, skipping", pmid)
                continue
            if doi and doi.lower() in existing_dois:
                logger.info("Rescue: DOI %s already in library, skipping", doi)
                continue

            record = pubmed_cache.get(pmid)
            if not record and doi:
                doi_lower = doi.lower()
                for r in pubmed_cache.values():
                    if r.get("doi", "").lower() == doi_lower:
                        record = r
                        break

            if not record or not record.get("title"):
                logger.warning("Rescue: no PubMed record for %s, marking failed", pmid or doi)
                failed.append(entry)
                continue

            target_cat, target_sub = run._first_context_pair(category, subcollection)
            collection_key = None
            if target_sub and target_cat:
                if target_cat == "6 - Genes" and genes_parent_key:
                    collection_key = zot.get_or_create_collection(target_sub, parent_key=genes_parent_key)
                elif target_cat:
                    cat_key = zot.get_or_create_collection(target_cat)
                    collection_key = zot.get_or_create_collection(target_sub, parent_key=cat_key)

            stats = zot.add_papers([record], collection_key=collection_key, source_tag="source:rescue")
            if stats.get("added", 0) > 0:
                uploaded += 1
                pmid_to_key.update(stats.get("pmid_to_key", {}))
                if record.get("pmid"):
                    existing_pmids.add(record["pmid"])
                if record.get("doi"):
                    existing_dois.add(record["doi"].lower())
                logger.info("Rescue: uploaded %s -> %s", pmid or doi, target_sub)
            else:
                logger.warning("Rescue: failed to upload %s", pmid or doi)
                failed.append(entry)

        logger.info("Rescue queue (PubMed): %d/%d uploaded, %d failed", uploaded, len(entries), len(failed))
        return uploaded, failed

    return rescue_fn


def main() -> None:
    runtime.configure_logging()

    p = argparse.ArgumentParser(description="Apply audit decisions using PubMed rescue")
    p.add_argument("--data-dir", default=os.path.join(_HERE, "site", "data"))
    p.add_argument("--work-dir", default=os.path.join(_HERE, "audit_work"))
    p.add_argument("--ledger", default=os.path.join(_HERE, "data", "audit_state.json"))
    p.add_argument("--log", default=os.path.join(_HERE, "data", "audit_log.json"))
    p.add_argument("--feedback", default=os.path.join(_HERE, "data", "audit_feedback.json"))
    p.add_argument("--genes-parent", default="6 - Genes")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--cache-file", default=None,
                   help="Path to a pre-built {pmid: record} JSON cache; skips PubMed HTTP fetch")
    args = p.parse_args()

    fp, fn = audit_bot.load_batch_items(args.work_dir)
    screen = audit_bot.load_verdicts(args.work_dir, "screen_")
    adj = audit_bot.load_verdicts(args.work_dir, "adj_")

    zot = runtime.make_zotero_client()

    baseline = audit_bot.load_dedup_baseline(args.work_dir)
    if baseline is not None:
        existing_pmids, existing_dois, pmid_to_key = baseline
    else:
        live = zot.get_dedup_baseline(include_trashed=False)
        pmid_to_key = live.pmid_to_key
        existing_pmids = live.existing_pmids
        existing_dois = live.existing_dois

    genes_parent_key = zot.get_or_create_collection(args.genes_parent)

    # Pre-compute the rescue list to fetch PubMed records before entering apply_actions
    plan_preview = audit_bot.compute_apply(fp, fn, screen, adj)
    rescue_pmids = [c.get("pmid", "") for c in plan_preview["to_rescue"] if c.get("pmid")]

    if args.cache_file:
        import json as _json
        with open(args.cache_file) as _f:
            _raw = _json.load(_f)
        # JSON deserializes tuples as lists; add_papers expects (first, last) tuples
        pubmed_cache = {
            pmid: {**rec, "authors": [tuple(a) if isinstance(a, list) else a for a in rec.get("authors", [])]}
            for pmid, rec in _raw.items()
        }
        logger.info("Loaded PubMed cache from %s: %d records", args.cache_file, len(pubmed_cache))
    else:
        logger.info("Pre-fetching %d PMIDs from PubMed", len(rescue_pmids))
        pubmed_cache = fetch_pubmed_records(rescue_pmids) if rescue_pmids and not args.dry_run else {}
        logger.info("PubMed cache: %d records fetched", len(pubmed_cache))

    rescue_fn = make_pubmed_rescue_fn(zot, existing_pmids, existing_dois, pmid_to_key, genes_parent_key, pubmed_cache)

    summary = audit_bot.apply_actions(
        fp, fn, screen, adj,
        zot=zot,
        rescue_fn=rescue_fn,
        ledger_path=args.ledger,
        log_path=args.log,
        apply=(not args.dry_run),
        feedback_path=args.feedback,
    )
    print(
        f"apply (pubmed-rescue): trashed={summary['trashed']} "
        f"would_trash={summary['would_trash']} "
        f"rescued={summary['rescued']} kept={summary['kept']} "
        f"judged={summary['judged']} "
        f"failed_trash={summary['failed_trash']} "
        f"failed_rescue={summary['failed_rescue']}"
    )


if __name__ == "__main__":
    main()
