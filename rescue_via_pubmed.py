"""Direct rescue script: fetch metadata from PubMed (not OpenAlex) and upload to Zotero.

Run after audit_bot.py --collect has written the verdicts but --apply is blocked
by OpenAlex rate limiting. This script:
  1. Determines rescue candidates via compute_apply()
  2. Batch-fetches PubMed XML for all PMIDs (E-fetch, no per-IP throttle)
  3. Converts to the same record format as OpenAlexClient.work_to_record()
  4. Uploads via add_papers() and writes the audit ledger/log/feedback

Usage:
    python rescue_via_pubmed.py [--dry-run] [--work-dir audit_work]
"""
import argparse
import datetime
import json
import os
import time
import xml.etree.ElementTree as ET

import requests

import audit_bot
from genebot import runtime
from run import _first_context_pair, load_genes_config

_HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# PubMed helpers
# ---------------------------------------------------------------------------

EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PUBMED_DELAY = 0.35  # 3 req/s for anonymous (NCBI limit)
BATCH_SIZE = 100


def _text(el, path, default=""):
    node = el.find(path)
    return (node.text or default).strip() if node is not None else default


def _efetch_pmids(pmids: list[str]) -> dict[str, dict]:
    """Fetch PubMed records for a list of PMIDs; return {pmid: record_dict}."""
    records = {}
    for i in range(0, len(pmids), BATCH_SIZE):
        batch = pmids[i : i + BATCH_SIZE]
        params = {
            "db": "pubmed",
            "id": ",".join(batch),
            "rettype": "xml",
            "retmode": "xml",
        }
        for attempt in range(3):
            try:
                resp = requests.get(EFETCH_URL, params=params, timeout=30)
                if resp.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt == 2:
                    print(f"  efetch failed: {e}")
                    resp = None
                time.sleep(2 ** attempt)
        else:
            continue

        if resp is None:
            continue

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            print(f"  XML parse error: {e}")
            continue

        for article_el in root.findall(".//PubmedArticle"):
            pmid_el = article_el.find(".//PMID")
            if pmid_el is None:
                continue
            pmid = pmid_el.text.strip()
            records[pmid] = _parse_pubmed_article(article_el)

        time.sleep(PUBMED_DELAY)

    return records


def _parse_pubmed_article(el) -> dict:
    """Convert a PubmedArticle XML element to a record dict."""
    medline = el.find("MedlineCitation")
    article = medline.find("Article") if medline is not None else None
    if article is None:
        return {}

    # Title
    title_el = article.find("ArticleTitle")
    title = ET.tostring(title_el, encoding="unicode", method="text").strip() if title_el is not None else ""

    # Abstract
    abstract_parts = []
    for text_el in article.findall(".//AbstractText"):
        label = text_el.get("Label", "")
        text = ET.tostring(text_el, encoding="unicode", method="text").strip()
        if label:
            abstract_parts.append(f"{label}: {text}")
        elif text:
            abstract_parts.append(text)
    abstract = " ".join(abstract_parts)

    # Authors
    authors = []
    for author_el in article.findall(".//Author"):
        last = _text(author_el, "LastName")
        fore = _text(author_el, "ForeName") or _text(author_el, "Initials")
        if last:
            authors.append((fore, last))

    # Journal
    journal_el = article.find(".//Journal")
    journal = ""
    journal_abbr = ""
    if journal_el is not None:
        journal = _text(journal_el, "Title")
        journal_abbr = _text(journal_el, "ISOAbbreviation")

    # Date
    year = ""
    date_published = ""
    pub_date = article.find(".//PubDate")
    if pub_date is not None:
        year = _text(pub_date, "Year")
        month = _text(pub_date, "Month")
        day = _text(pub_date, "Day")
        if year and month and day:
            date_published = f"{year}-{month}-{day}"
        elif year and month:
            date_published = f"{year}-{month}"
        elif year:
            date_published = year

    # Volume / issue / pages
    volume = _text(article, ".//Volume")
    issue = _text(article, ".//Issue")
    pages = _text(article, ".//MedlinePgn")

    # DOI
    doi = ""
    for id_el in article.findall(".//ArticleId"):
        if id_el.get("IdType") == "doi":
            doi = (id_el.text or "").strip()
            break

    # MeSH
    mesh_terms = []
    for mesh_el in (medline.findall(".//MeshHeading/DescriptorName") if medline is not None else []):
        name = (mesh_el.text or "").strip()
        if name:
            mesh_terms.append(name)

    # Publication type
    pub_types = []
    for pt_el in article.findall(".//PublicationType"):
        pt = (pt_el.text or "").strip()
        if pt:
            pub_types.append(pt)

    return {
        "pmid": _text(medline, "PMID") if medline is not None else "",
        "doi": doi,
        "title": title,
        "authors": authors,
        "journal": journal,
        "journal_abbr": journal_abbr,
        "year": year,
        "date_published": date_published,
        "abstract": abstract,
        "publication_type": pub_types,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "cited_by_count": 0,
        "mesh_terms": mesh_terms,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    runtime.configure_logging()
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--work-dir", default=os.path.join(_HERE, "audit_work"))
    p.add_argument("--ledger", default=os.path.join(_HERE, "data", "audit_state.json"))
    p.add_argument("--log", default=os.path.join(_HERE, "data", "audit_log.json"))
    p.add_argument("--feedback", default=os.path.join(_HERE, "data", "audit_feedback.json"))
    p.add_argument("--prefetched", default=None,
                   help="Path to pre-fetched pubmed_records.json; skips efetch API calls")
    args = p.parse_args()

    apply = not args.dry_run

    fp, fn = audit_bot.load_batch_items(args.work_dir)
    screen = audit_bot.load_verdicts(args.work_dir, "screen_")
    adj = audit_bot.load_verdicts(args.work_dir, "adj_")
    plan = audit_bot.compute_apply(fp, fn, screen, adj)

    entries = audit_bot.build_rescue_entries(plan["to_rescue"])
    print(f"rescue candidates: {len(entries)}")

    # Dedup baseline
    zot = runtime.make_zotero_client()
    baseline = audit_bot.load_dedup_baseline(args.work_dir)
    if baseline is not None:
        existing_pmids, existing_dois, pmid_to_key = baseline
    else:
        live = zot.get_dedup_baseline(include_trashed=False)
        pmid_to_key = live.pmid_to_key
        existing_pmids = live.existing_pmids
        existing_dois = live.existing_dois

    # Filter to items not already in library
    to_fetch = []
    for e in entries:
        pmid = (e.get("pmid") or "").strip()
        doi = (e.get("doi") or "").strip().lower()
        if pmid and pmid in existing_pmids:
            print(f"  {pmid} already in library, skipping")
            continue
        if doi and doi in existing_dois:
            print(f"  doi:{doi} already in library, skipping")
            continue
        to_fetch.append(e)

    print(f"to fetch from PubMed: {len(to_fetch)}")
    if not to_fetch:
        print("Nothing to rescue.")
        return

    pmids = [e["pmid"] for e in to_fetch if e.get("pmid")]
    if args.prefetched:
        print(f"loading pre-fetched records from {args.prefetched}...")
        with open(args.prefetched, encoding="utf-8") as _f:
            pubmed_records = json.load(_f)
        print(f"loaded {len(pubmed_records)} pre-fetched records")
    else:
        print(f"fetching {len(pmids)} PMIDs from PubMed...")
        pubmed_records = _efetch_pmids(pmids)
        print(f"fetched {len(pubmed_records)} records")

    genes_parent_key = None
    rescue_cfg = load_genes_config()
    genes_parent_name = rescue_cfg.get("collections", {}).get("genes_parent")
    if genes_parent_name:
        try:
            genes_parent_key = zot.get_or_create_collection(genes_parent_name)
        except Exception as exc:
            print(f"  genes_parent collection error: {exc}")

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log = audit_bot.load_json_list(args.log)
    uploaded = 0
    failed_ids = set()

    for entry in to_fetch:
        pmid = (entry.get("pmid") or "").strip()
        doi = (entry.get("doi") or "").strip()
        record = pubmed_records.get(pmid)
        if not record or not record.get("title"):
            print(f"  no PubMed record for {pmid or doi}, skipping")
            failed_ids.add(entry.get("id", pmid))
            continue

        # Fill in DOI from entry if PubMed didn't return one
        if not record.get("doi") and doi:
            record["doi"] = doi

        subcollection = (entry.get("subcollection") or "").strip()
        category = (entry.get("category") or "").strip()
        target_cat, target_sub = _first_context_pair(category, subcollection)

        collection_key = None
        if apply and target_sub and target_cat:
            try:
                if target_cat == "6 - Genes" and genes_parent_key:
                    collection_key = zot.get_or_create_collection(target_sub, parent_key=genes_parent_key)
                elif target_cat:
                    cat_key = zot.get_or_create_collection(target_cat)
                    collection_key = zot.get_or_create_collection(target_sub, parent_key=cat_key)
            except Exception as exc:
                print(f"  collection error for {target_sub}: {exc}")

        if not apply:
            print(f"  [dry-run] would upload {pmid} -> {target_sub or 'unknown'}")
            continue

        stats = zot.add_papers([record], collection_key=collection_key, source_tag="source:rescue")
        if stats.get("added", 0) > 0:
            uploaded += 1
            if record.get("pmid"):
                existing_pmids.add(record["pmid"])
            if record.get("doi"):
                existing_dois.add(record["doi"].lower())
            print(f"  uploaded {pmid} -> {target_sub}")
        else:
            print(f"  upload failed for {pmid}")
            failed_ids.add(entry.get("id", pmid))

        cid = entry.get("id", f"pmid:{pmid}")
        log.append({
            "ts": ts, "direction": "fn", "action": "rescue", "id": cid,
            "pmid": pmid, "gene_or_topic": entry.get("subcollection", ""),
            "screener_verdict": screen.get(cid),
            "adjudicator_verdict": adj.get(cid),
            "applied": apply,
            "models": {"screener": "haiku", "adjudicator": "sonnet"},
        })

    print(f"\nrescued {uploaded}/{len(to_fetch)} items")

    if not apply:
        return

    # Handle the trash side too
    trash_keys = plan["to_trash_keys"]
    if trash_keys:
        print(f"trashing {len(trash_keys)} FP items...")
        trash_result = zot.trash_items(trash_keys, apply=True)
        print(f"  trashed={trash_result['trashed']} failed={trash_result['failed']}")
        fp_by_id = {c["id"]: c for c in fp}
        key_to_id = {c["key"]: c["id"] for c in fp if c.get("key")}
        for key in trash_keys:
            cid = key_to_id.get(key, "")
            log.append({
                "ts": ts, "direction": "fp", "action": "trash", "id": cid,
                "key": key, "gene_or_topic": fp_by_id.get(cid, {}).get("gene_or_topic", ""),
                "screener_verdict": screen.get(cid),
                "adjudicator_verdict": adj.get(cid),
                "applied": True,
                "models": {"screener": "haiku", "adjudicator": "sonnet"},
            })

    # Write audit log
    os.makedirs(os.path.dirname(args.log) or ".", exist_ok=True)
    with open(args.log, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=1)

    # Update ledger (exclude permanently failed rescues)
    audited = audit_bot.load_ledger(args.ledger) | (plan["judged_ids"] - failed_ids)
    audit_bot.save_ledger(args.ledger, audited, now=ts)

    # Feedback
    if args.feedback:
        audit_bot.update_feedback(args.feedback, plan, fp, now=ts)

    print(f"\napply complete: rescued={uploaded} judged={len(plan['judged_ids'])} failed_rescue={len(failed_ids)}")


if __name__ == "__main__":
    main()
