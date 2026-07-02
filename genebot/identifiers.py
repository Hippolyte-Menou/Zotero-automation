"""Canonical extraction of article identifiers from Zotero item data.

One home for "pull a PMID / DOI / PMCID out of a Zotero item's ``data`` dict".
Previously this logic lived as private static methods on ``ZoteroGroupClient``
(reached into from ``dedup_library``) *and* was reimplemented, with slightly
different rules, in ``pdf_helpers``. Centralising it here means dedup, PDF
fetching, the audit bot, and the client all agree on what a valid identifier
looks like, and there is a single test surface.

Every function takes the inner ``data`` dict of a Zotero item (i.e.
``item["data"]``) and returns a normalised bare identifier, or ``""`` when the
item carries none.
"""

import re

# PMID prefixes: "PMID: 123", "PMID:123", "pmid 123", "PubMed ID: 123"
_RE_PMID_PREFIX = re.compile(r"(?:pmid|pubmed\s*id)\s*:?\s*(\d+)", re.IGNORECASE)
# Current PubMed URLs: pubmed.ncbi.nlm.nih.gov/12345
_RE_PUBMED_NEW = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")
# Old-style PubMed URLs: ncbi.nlm.nih.gov/pubmed/12345
_RE_PUBMED_OLD = re.compile(r"ncbi\.nlm\.nih\.gov/pubmed/(\d+)")
# DOI embedded in a free-text field (extra): "doi: 10.1/x", "DOI:10.1/x"
_RE_DOI_TEXT = re.compile(r"doi\s*:?\s*(10\.\S+)", re.IGNORECASE)
# DOI embedded in a URL: doi.org/10.1/x
_RE_DOI_URL = re.compile(r"doi\.org/(10\.\S+)")
# PMCID in the extra field: "PMCID: PMC12345"
_RE_PMCID = re.compile(r"PMCID:\s*(PMC\d+)")


def extract_pmid(data: dict) -> str:
    """Extract a bare PMID from a Zotero item's ``data`` dict.

    Checks, in order: the ``extra`` field for a PMID prefix, the ``url`` field
    for either PubMed URL format, then the ``extra`` field for an embedded
    PubMed URL (some translators dump URLs there). Returns ``""`` if none.
    """
    extra = data.get("extra", "")
    if extra:
        m = _RE_PMID_PREFIX.search(extra)
        if m:
            return m.group(1)

    url = data.get("url", "")
    if url:
        for pattern in (_RE_PUBMED_NEW, _RE_PUBMED_OLD):
            m = pattern.search(url)
            if m:
                return m.group(1)

    if extra:
        for pattern in (_RE_PUBMED_NEW, _RE_PUBMED_OLD):
            m = pattern.search(extra)
            if m:
                return m.group(1)

    return ""


def extract_doi(data: dict) -> str:
    """Extract and normalise a DOI from a Zotero item's ``data`` dict.

    Checks the native ``DOI`` field first, then the ``extra`` field, then a
    ``doi.org`` URL in the ``url`` field. Returns a bare DOI (no URL prefix) in
    lowercase, or ``""``. The ``url`` fallback unifies the old ``pdf_helpers``
    behaviour into this one extractor.
    """
    raw = data.get("DOI", "")
    if not raw:
        extra = data.get("extra", "")
        if extra:
            m = _RE_DOI_TEXT.search(extra)
            if m:
                raw = m.group(1)
    if not raw:
        url = data.get("url", "")
        if url:
            m = _RE_DOI_URL.search(url)
            if m:
                raw = m.group(1)
    if raw:
        return raw.replace("https://doi.org/", "").replace("http://doi.org/", "").strip().lower()
    return ""


def extract_pmcid(data: dict) -> str:
    """Extract a PMCID (``PMC\\d+``) from a Zotero item's ``extra`` field, or ``""``."""
    m = _RE_PMCID.search(data.get("extra", ""))
    return m.group(1) if m else ""
