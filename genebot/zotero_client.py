"""Zotero group library interaction via pyzotero."""

import time
import logging
import httpx
from pyzotero import zotero
from pyzotero import errors as zotero_exceptions

logger = logging.getLogger(__name__)


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient errors that warrant a retry (5xx, timeouts)."""
    if isinstance(exc, (httpx.TimeoutException, httpx.ReadTimeout)):
        return True
    if isinstance(exc, zotero_exceptions.HTTPError):
        msg = str(exc)
        if "Code: 5" in msg:
            return True
        resp = getattr(exc, "response", None)
        if resp is not None and hasattr(resp, "status_code"):
            return 500 <= resp.status_code < 600
    return False


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
        # Override default timeout (httpx default is 5s, too short for large libraries)
        self.zot.client.timeout = httpx.Timeout(60.0, connect=15.0)
        self.delay = delay
        # Cache of (name, parent_key) -> key, populated lazily
        self._collection_cache: dict[tuple[str, str | None], str] = {}

    def _paginate_with_retry(
        self,
        method,
        label: str,
        max_retries: int = 3,
        **kwargs,
    ) -> list[dict] | None:
        """Paginate through a Zotero listing endpoint, retrying individual
        pages on transient errors instead of restarting from the beginning.

        *method* is a bound pyzotero method (e.g. ``self.zot.items``).
        Returns the full item list, or ``None`` if a page fails after all
        retries.
        """
        limit = 100
        start = 0
        all_items: list[dict] = []

        while True:
            page = None
            for attempt in range(max_retries):
                try:
                    page = method(limit=limit, start=start, **kwargs)
                    break
                except Exception as e:
                    if not _is_retryable(e):
                        raise
                    wait = 5 * (attempt + 1)
                    logger.warning(
                        f"Zotero transient error fetching {label} "
                        f"(start={start}, attempt {attempt + 1}/{max_retries}): "
                        f"{e}. Retrying in {wait}s..."
                    )
                    time.sleep(wait)

            if page is None:
                logger.error(
                    f"Failed to fetch {label} at start={start} "
                    f"after {max_retries} attempts"
                )
                return None

            all_items.extend(page)
            if len(page) < limit:
                break
            start += limit

        return all_items

    # ------------------------------------------------------------------
    # Collections
    # ------------------------------------------------------------------

    def _load_collection_cache(self) -> None:
        """Fetch all collections and populate the local cache."""
        collections = self.zot.everything(self.zot.collections())
        self._collection_cache = {}
        for c in collections:
            name = c["data"]["name"]
            parent = c["data"].get("parentCollection") or None
            self._collection_cache[(name, parent)] = c["data"]["key"]
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

        cache_key = (name, parent_key)
        if cache_key in self._collection_cache:
            return self._collection_cache[cache_key]

        # Create the collection
        payload: dict = {"name": name}
        if parent_key:
            payload["parentCollection"] = parent_key

        resp = self.zot.create_collections([payload])
        if resp.get("successful"):
            key = list(resp["successful"].values())[0]["data"]["key"]
            logger.info(f"Created collection '{name}' (parent={parent_key}) -> key {key}")
            self._collection_cache[(name, parent_key)] = key
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

    @staticmethod
    def _extract_pmid_from_data(data: dict) -> str:
        """Extract PMID from a Zotero item's data dict (extra field or PubMed URL)."""
        extra = data.get("extra", "")
        for line in extra.split("\n"):
            line = line.strip()
            if line.upper().startswith("PMID:"):
                return line.split(":", 1)[1].strip()
        url = data.get("url", "")
        if "pubmed.ncbi.nlm.nih.gov" in url:
            parts = url.rstrip("/").split("/")
            if parts and parts[-1].isdigit():
                return parts[-1]
        return ""

    def get_existing_items(self) -> dict[str, str]:
        """
        Retrieve all PMIDs already in the group library mapped to their Zotero item keys.
        Scans the 'extra' field and PubMed URLs for PMID entries.
        Retries individual pages on transient errors.

        Returns {pmid: zotero_item_key}.
        """
        logger.info("Fetching existing library items for deduplication...")
        pmid_to_key: dict[str, str] = {}

        items = self._paginate_with_retry(
            self.zot.items, "library items", itemType="-attachment"
        )
        if items is None:
            return pmid_to_key

        for item in items:
            data = item.get("data", {})
            zot_key = data.get("key", "")

            pmid = self._extract_pmid_from_data(data)

            if pmid and zot_key:
                pmid_to_key[pmid] = zot_key

        logger.info(f"Found {len(pmid_to_key)} existing PMIDs in library")
        return pmid_to_key

    def get_existing_pmids(self) -> set[str]:
        """Return the set of PMIDs already in the library. Wraps get_existing_items()."""
        return set(self.get_existing_items().keys())

    def get_trashed_pmids(self) -> set[str]:
        """Return PMIDs of all items currently in the group library trash.

        Trashed PMIDs are used to block re-upload of deliberately deleted papers.
        Retries individual pages on transient errors.
        """
        logger.info("Fetching trashed items...")
        trashed: set[str] = set()

        items = self._paginate_with_retry(self.zot.trash, "trash")
        if items is None:
            return trashed

        for item in items:
            data = item.get("data", {})
            pmid = self._extract_pmid_from_data(data)
            if pmid:
                trashed.add(pmid)

        logger.info(f"Found {len(trashed)} PMIDs in trash")
        return trashed

    def get_collection_pmids(self, collection_key: str) -> list[str]:
        """Return all PMIDs for items in a specific collection.

        Used to seed citation expansion from the existing curated library
        for a gene, avoiding redundant OpenAlex searches.
        """
        pmids: list[str] = []

        items = self._paginate_with_retry(
            lambda **kw: self.zot.collection_items(collection_key, **kw),
            f"collection {collection_key}",
            itemType="-attachment",
        )
        if items is None:
            return pmids

        for item in items:
            data = item.get("data", {})
            pmid = self._extract_pmid_from_data(data)
            if pmid:
                pmids.append(pmid)

        return pmids

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
        stats: dict = {"added": 0, "failed": 0, "skipped_no_data": 0, "pmid_to_key": {}}

        items_to_create = []
        ordered_pmids: list[str] = []  # parallel to items_to_create for key mapping
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

            # MeSH descriptor names as additional tags (plain Title Case)
            for mesh_term in r.get("mesh_terms", []):
                tag_strings.append(mesh_term)

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

            for author_entry in r.get("authors", []):
                # authors is a list of (firstName, lastName) tuples produced
                # by OpenAlexClient.work_to_record() -- name splitting and
                # comma-form detection happen there.
                if isinstance(author_entry, tuple):
                    first_name, last_name = author_entry
                else:
                    # Legacy fallback: plain string "First Last"
                    parts = author_entry.rsplit(" ", 1)
                    first_name = parts[0] if len(parts) == 2 else ""
                    last_name = parts[1] if len(parts) == 2 else author_entry
                item["creators"].append({
                    "creatorType": "author",
                    "firstName": first_name,
                    "lastName": last_name,
                })

            if collection_key:
                item["collections"] = [collection_key]

            items_to_create.append(item)
            ordered_pmids.append(r.get("pmid") or "")

        # Push in batches of 50 (Zotero API limit)
        batch_size = 50
        for i in range(0, len(items_to_create), batch_size):
            batch = items_to_create[i : i + batch_size]
            logger.info(f"Uploading batch {i // batch_size + 1} ({len(batch)} items)")

            for attempt in range(3):
                try:
                    resp = self.zot.create_items(batch)
                    n_success = len(resp.get("successful", {}))
                    n_failed = len(resp.get("failed", {}))
                    stats["added"] += n_success
                    stats["failed"] += n_failed

                    # Capture {pmid: zotero_key} for successfully created items
                    for idx_str, item_data in resp.get("successful", {}).items():
                        zot_key = item_data.get("data", {}).get("key", "")
                        pmid = ordered_pmids[i + int(idx_str)]
                        if zot_key and pmid:
                            stats["pmid_to_key"][pmid] = zot_key

                    if n_failed > 0:
                        for idx, err in resp["failed"].items():
                            logger.warning(f"  Failed item {idx}: {err}")
                    break

                except (httpx.TimeoutException, httpx.ReadTimeout) as e:
                    if attempt == 0:
                        wait = 5
                        logger.warning(
                            f"Batch upload timeout (attempt 1/3): {e}. "
                            f"Retrying in {wait}s..."
                        )
                        time.sleep(wait)
                    else:
                        logger.warning(
                            f"Batch upload timeout on retry (attempt {attempt + 1}/3): {e}. "
                            f"Skipping further retries to avoid duplicates. "
                            f"{len(batch)} items may need manual verification."
                        )
                        stats["failed"] += len(batch)
                        break

                except Exception as e:
                    stats["failed"] += len(batch)
                    logger.error(f"Batch upload error: {e}")
                    break

            time.sleep(self.delay)

        return stats

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    def add_relations(self, item_key: str, related_keys: list[str]) -> None:
        """Add dc:relation links between item_key and each key in related_keys.

        Relations are set bidirectionally: item_key -> related_keys and each
        related_key -> item_key. Existing relations are preserved (set merge).
        Skips patching if nothing new to add.
        """
        base_uri = f"http://zotero.org/groups/{self.zot.library_id}/items"

        def _patch(key: str, add_uris: list[str]) -> None:
            try:
                item = self.zot.item(key)
            except Exception as e:
                logger.warning(f"add_relations: could not fetch item {key}: {e}")
                return
            existing = item["data"].get("relations", {}).get("dc:relation", [])
            if isinstance(existing, str):
                existing = [existing]
            merged = list(set(existing) | set(add_uris))
            if set(merged) == set(existing):
                return  # nothing new
            item["data"].setdefault("relations", {})["dc:relation"] = merged
            try:
                self.zot.update_item(item)
            except Exception as e:
                logger.warning(f"add_relations: could not patch item {key}: {e}")

        source_uri = f"{base_uri}/{item_key}"
        _patch(item_key, [f"{base_uri}/{k}" for k in related_keys])
        for rk in related_keys:
            _patch(rk, [source_uri])
