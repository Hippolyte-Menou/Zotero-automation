"""Zotero group library interaction via pyzotero."""

import time
import logging
from pyzotero import zotero

logger = logging.getLogger(__name__)

# Mapping from OpenAlex work type strings to normalised tag names.
_TYPE_TAG_MAP = {
    "review": "review",
    "editorial": "editorial",
    "letter": "letter",
    "erratum": "erratum",
}


class ZoteroGroupClient:
    def __init__(self, group_id: str, api_key: str, delay: float = 1.0):
        self.zot = zotero.Zotero(group_id, "group", api_key)
        self.delay = delay
        # Cache of collection name -> key, populated lazily
        self._collection_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Collections
    # ------------------------------------------------------------------

    def _load_collection_cache(self) -> None:
        """Fetch all collections and populate the local cache."""
        collections = self.zot.everything(self.zot.collections())
        self._collection_cache = {
            c["data"]["name"]: c["data"]["key"] for c in collections
        }
        logger.debug(f"Collection cache loaded: {len(self._collection_cache)} collections")

    def get_or_create_collection(
        self,
        name: str,
        parent_key: str | None = None,
    ) -> str:
        """
        Return the key for a collection with the given name.
        If it does not exist, create it (optionally nested under parent_key).
        Safe to call multiple times -- never creates duplicates.
        """
        if not self._collection_cache:
            self._load_collection_cache()

        if name in self._collection_cache:
            return self._collection_cache[name]

        # Create the collection
        payload: dict = {"name": name}
        if parent_key:
            payload["parentCollection"] = parent_key

        resp = self.zot.create_collections([payload])
        if resp.get("successful"):
            key = list(resp["successful"].values())[0]["data"]["key"]
            logger.info(f"Created collection '{name}' (parent={parent_key}) -> key {key}")
            self._collection_cache[name] = key
            return key
        else:
            raise RuntimeError(f"Failed to create collection '{name}': {resp}")

    def ensure_collection_path(self, *names: str) -> str:
        """
        Ensure a chain of nested collections exists and return the leaf key.
        Example: ensure_collection_path("6 - Genes", "CRB1")
          -> creates "6 - Genes" if needed, then "CRB1" under it.
        """
        parent_key: str | None = None
        for name in names:
            parent_key = self.get_or_create_collection(name, parent_key=parent_key)
        return parent_key  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def get_existing_pmids(self) -> set[str]:
        """
        Retrieve all PMIDs already in the group library.
        Scans the 'extra' field and PubMed URLs for PMID entries.
        """
        logger.info("Fetching existing library items for deduplication...")
        existing_pmids = set()

        items = self.zot.everything(self.zot.items(itemType="-attachment"))

        for item in items:
            data = item.get("data", {})

            # Check 'extra' field for PMID
            extra = data.get("extra", "")
            for line in extra.split("\n"):
                line = line.strip()
                if line.upper().startswith("PMID:"):
                    pmid = line.split(":", 1)[1].strip()
                    existing_pmids.add(pmid)

            # Check URL field for PubMed URL
            url = data.get("url", "")
            if "pubmed.ncbi.nlm.nih.gov" in url:
                parts = url.rstrip("/").split("/")
                if parts:
                    candidate = parts[-1]
                    if candidate.isdigit():
                        existing_pmids.add(candidate)

        logger.info(f"Found {len(existing_pmids)} existing PMIDs in library")
        return existing_pmids

    def add_papers(
        self,
        records: list[dict],
        collection_key: str | None = None,
        gene_symbol: str | None = None,
        extra_tags: list[str] | None = None,
        source_tag: str | None = None,
    ) -> dict:
        """
        Add papers to the Zotero group library in batches of 50.

        Accepts records produced by OpenAlexClient.work_to_record().

        Tags applied to every item:
          - gene symbol (if provided)
          - work type tag (from OpenAlex type field)
          - any extra_tags passed in (disease groups etc.)
          - source_tag for provenance tracking
        """
        stats = {"added": 0, "failed": 0, "skipped_no_data": 0}

        items_to_create = []
        for r in records:
            if not r.get("title"):
                stats["skipped_no_data"] += 1
                continue

            # Build tag list
            tag_strings: list[str] = []
            if gene_symbol:
                tag_strings.append(gene_symbol)

            # Work type tag from OpenAlex
            for pt in r.get("publication_type", []):
                tag = _TYPE_TAG_MAP.get(pt)
                if tag:
                    tag_strings.append(tag)

            if extra_tags:
                tag_strings.extend(extra_tags)
            if source_tag:
                tag_strings.append(source_tag)

            # Deduplicate while preserving order
            seen: set[str] = set()
            unique_tags = []
            for t in tag_strings:
                if t not in seen:
                    unique_tags.append({"tag": t})
                    seen.add(t)

            item = {
                "itemType": "journalArticle",
                "title": r["title"],
                "abstractNote": r.get("abstract", ""),
                "publicationTitle": r.get("journal", ""),
                "journalAbbreviation": r.get("journal_abbr", ""),
                "volume": r.get("volume", ""),
                "issue": r.get("issue", ""),
                "pages": r.get("pages", ""),
                "date": r.get("date_published", ""),
                "DOI": r.get("doi", ""),
                "url": (
                    f"https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/"
                    if r.get("pmid")
                    else ""
                ),
                "extra": f"PMID: {r['pmid']}" if r.get("pmid") else "",
                "creators": [],
                "tags": unique_tags,
            }

            for author_name in r.get("authors", []):
                parts = author_name.rsplit(" ", 1)
                if len(parts) == 2:
                    item["creators"].append({
                        "creatorType": "author",
                        "lastName": parts[0],
                        "firstName": parts[1],
                    })
                else:
                    item["creators"].append({
                        "creatorType": "author",
                        "lastName": author_name,
                        "firstName": "",
                    })

            if collection_key:
                item["collections"] = [collection_key]

            items_to_create.append(item)

        # Push in batches of 50 (Zotero API limit)
        batch_size = 50
        for i in range(0, len(items_to_create), batch_size):
            batch = items_to_create[i : i + batch_size]
            logger.info(f"Uploading batch {i // batch_size + 1} ({len(batch)} items)")

            try:
                resp = self.zot.create_items(batch)
                n_success = len(resp.get("successful", {}))
                n_failed = len(resp.get("failed", {}))
                stats["added"] += n_success
                stats["failed"] += n_failed

                if n_failed > 0:
                    for idx, err in resp["failed"].items():
                        logger.warning(f"  Failed item {idx}: {err}")

            except Exception as e:
                stats["failed"] += len(batch)
                logger.error(f"Batch upload error: {e}")

            time.sleep(self.delay)

        return stats
