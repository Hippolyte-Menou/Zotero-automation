"""Zotero group library interaction via pyzotero."""

import time
import logging
from pyzotero import zotero

logger = logging.getLogger(__name__)


class ZoteroGroupClient:
    def __init__(self, group_id: str, api_key: str, delay: float = 1.0):
        self.zot = zotero.Zotero(group_id, "group", api_key)
        self.delay = delay

    def get_existing_pmids(self) -> set[str]:
        """
        Retrieve all PMIDs already in the group library.
        Scans the 'extra' field for PMID entries.
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

    def add_papers(self, records: list[dict], collection_key: str | None = None) -> dict:
        """
        Add papers to the Zotero group library in batches of 50.
        Creates journal article items from PubMed metadata.
        """
        stats = {"added": 0, "failed": 0, "skipped_no_data": 0}

        # Build all items first
        items_to_create = []
        for r in records:
            if not r.get("title"):
                stats["skipped_no_data"] += 1
                continue

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
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/" if r.get("pmid") else "",
                "extra": f"PMID: {r['pmid']}" if r.get("pmid") else "",
                "creators": [],
                "tags": [],
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

    def get_or_create_collection(self, name: str) -> str:
        """Get collection key by name, or create it."""
        collections = self.zot.collections()
        for c in collections:
            if c["data"]["name"] == name:
                return c["data"]["key"]

        resp = self.zot.create_collections([{"name": name}])
        if resp.get("successful"):
            key = list(resp["successful"].values())[0]["data"]["key"]
            logger.info(f"Created collection '{name}' with key {key}")
            return key
        else:
            raise RuntimeError(f"Failed to create collection '{name}': {resp}")
