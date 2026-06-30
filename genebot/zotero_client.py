"""Zotero group library interaction via pyzotero."""

import re
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

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    # Regex patterns for PMID extraction (compiled once at class level)
    # Matches: "PMID: 123", "PMID:123", "pmid 123", "PubMed ID: 123"
    _RE_PMID_PREFIX = re.compile(
        r"(?:pmid|pubmed\s*id)\s*:?\s*(\d+)", re.IGNORECASE
    )
    # Matches current PubMed URLs: pubmed.ncbi.nlm.nih.gov/12345
    _RE_PUBMED_NEW = re.compile(
        r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)"
    )
    # Matches old-style PubMed URLs: ncbi.nlm.nih.gov/pubmed/12345
    _RE_PUBMED_OLD = re.compile(
        r"ncbi\.nlm\.nih\.gov/pubmed/(\d+)"
    )

    @staticmethod
    def _extract_pmid_from_data(data: dict) -> str:
        """Extract PMID from a Zotero item's data dict.

        Checks (in order):
        1. Extra field for PMID prefix variants (PMID:, pmid, PubMed ID:, etc.)
        2. URL field for PubMed URLs (current and old-style domains)
        3. Extra field for embedded PubMed URLs
        """
        cls = ZoteroGroupClient

        extra = data.get("extra", "")
        if extra:
            m = cls._RE_PMID_PREFIX.search(extra)
            if m:
                return m.group(1)

        # Check url field for both PubMed URL formats
        url = data.get("url", "")
        if url:
            for pattern in (cls._RE_PUBMED_NEW, cls._RE_PUBMED_OLD):
                m = pattern.search(url)
                if m:
                    return m.group(1)

        # Check extra field for embedded PubMed URLs (some translators dump URLs there)
        if extra:
            for pattern in (cls._RE_PUBMED_NEW, cls._RE_PUBMED_OLD):
                m = pattern.search(extra)
                if m:
                    return m.group(1)

        return ""

    @staticmethod
    def _extract_doi_from_data(data: dict) -> str:
        """Extract and normalise DOI from a Zotero item's data dict.

        Checks the native DOI field first, then falls back to the extra field.
        Returns a bare DOI (no URL prefix) in lowercase, or empty string.
        """
        raw = data.get("DOI", "")
        if not raw:
            extra = data.get("extra", "")
            if extra:
                m = re.search(r"doi\s*:?\s*(10\.\S+)", extra, re.IGNORECASE)
                if m:
                    raw = m.group(1)
        if raw:
            return raw.replace("https://doi.org/", "").strip().lower()
        return ""

    def get_existing_items(self) -> dict[str, str]:
        """
        Retrieve all PMIDs already in the group library mapped to their Zotero item keys.
        Scans the 'extra' field and PubMed URLs for PMID entries.
        Also populates ``doi_to_key`` for secondary dedup.
        Retries individual pages on transient errors.

        Returns {pmid: zotero_item_key}.
        """
        logger.info("Fetching existing library items for deduplication...")
        pmid_to_key: dict[str, str] = {}
        self._doi_to_key: dict[str, str] = {}

        items = self._paginate_with_retry(
            self.zot.items, "library items", itemType="-attachment"
        )
        if items is None:
            raise RuntimeError(
                "Failed to fetch existing library items for deduplication "
                "after retries. Refusing to continue with an empty dedup "
                "baseline, which would re-upload the entire library as "
                "duplicates."
            )

        for item in items:
            data = item.get("data", {})
            zot_key = data.get("key", "")

            pmid = self._extract_pmid_from_data(data)
            doi = self._extract_doi_from_data(data)

            if pmid and zot_key:
                pmid_to_key[pmid] = zot_key
            if doi and zot_key:
                self._doi_to_key[doi] = zot_key

        logger.info(
            f"Found {len(pmid_to_key)} existing PMIDs and "
            f"{len(self._doi_to_key)} existing DOIs in library"
        )
        return pmid_to_key

    def get_existing_dois(self) -> dict[str, str]:
        """Return {doi: zotero_item_key} collected by the last get_existing_items() call."""
        return getattr(self, "_doi_to_key", {})

    def get_trashed_pmids(self) -> set[str]:
        """Return PMIDs of all items currently in the group library trash.

        Trashed PMIDs are used to block re-upload of deliberately deleted papers.
        Also populates ``_trashed_dois`` for secondary DOI dedup.
        Retries individual pages on transient errors.
        """
        logger.info("Fetching trashed items...")
        trashed: set[str] = set()
        self._trashed_dois: set[str] = set()

        items = self._paginate_with_retry(self.zot.trash, "trash")
        if items is None:
            return trashed

        for item in items:
            data = item.get("data", {})
            pmid = self._extract_pmid_from_data(data)
            if pmid:
                trashed.add(pmid)
            doi = self._extract_doi_from_data(data)
            if doi:
                self._trashed_dois.add(doi)

        logger.info(f"Found {len(trashed)} PMIDs and {len(self._trashed_dois)} DOIs in trash")
        return trashed

    def get_trashed_dois(self) -> set[str]:
        """Return DOIs collected by the last get_trashed_pmids() call."""
        return getattr(self, "_trashed_dois", set())

    @staticmethod
    def _get_top_level_ancestor(key: str, parent_map: dict) -> str:
        """Walk up the collection parent chain and return the top-level ancestor key."""
        visited: set[str] = set()
        current = key
        while True:
            if current in visited:
                break  # cycle protection
            visited.add(current)
            parent = parent_map.get(current)
            if parent is None:
                return current
            current = parent
        return key  # fallback

    def get_all_items_full(self) -> list[dict]:
        """Fetch all library items with full metadata for inverse bot analysis.

        Returns a list of dicts with: pmid, doi, zotero_key, title, authors,
        year, journal, abstract, source_tags, category, subcollection.

        category and subcollection mirror the near-misses data model:
        - category: top-level Zotero collection name (comma-joined if multiple)
        - subcollection: direct collection name (comma-joined if multiple)

        Items without both pmid and doi are skipped (cannot be matched on
        OpenAlex for centrality computation).
        """
        logger.info("Fetching full library metadata for inverse bot analysis...")

        # Build collection key -> name map and parent map
        coll_map: dict[str, str] = {}
        parent_map: dict[str, str | None] = {}
        try:
            collections = self.zot.everything(self.zot.collections())
            for c in collections:
                key = c["data"].get("key", "")
                name = c["data"].get("name", "")
                # pyzotero returns False (not None) for root collections
                raw_parent = c["data"].get("parentCollection") or None
                if key:
                    if name:
                        coll_map[key] = name
                    parent_map[key] = raw_parent
        except Exception as e:
            logger.warning(
                f"Could not fetch collections: {e}. Category names will be raw keys."
            )

        items = self._paginate_with_retry(
            self.zot.items, "all library items (full)", itemType="-attachment"
        )
        if items is None:
            logger.error("Failed to fetch library items for inverse bot")
            return []

        result: list[dict] = []
        for item in items:
            data = item.get("data", {})
            zot_key = item.get("key", "") or data.get("key", "")

            pmid = self._extract_pmid_from_data(data)
            doi = self._extract_doi_from_data(data)

            if not pmid and not doi:
                continue  # Cannot match on OpenAlex

            # Authors
            authors: list[list[str]] = []
            for creator in data.get("creators", []):
                if creator.get("creatorType") == "author":
                    first = creator.get("firstName", "")
                    last = creator.get("lastName", "")
                    authors.append([first, last])

            # Year from date field
            year = ""
            date_str = data.get("date", "")
            if date_str:
                m = re.search(r"\d{4}", date_str)
                if m:
                    year = m.group(0)

            # Source tags (provenance)
            source_tags = [
                t["tag"] for t in data.get("tags", [])
                if t.get("tag", "").startswith("source:")
            ]

            # Resolve collection keys to category (top-level) and subcollection (leaf)
            seen_cats: dict[str, None] = {}
            seen_subs: dict[str, None] = {}
            for k in data.get("collections", []):
                sub_name = coll_map.get(k, k)
                anc_key = self._get_top_level_ancestor(k, parent_map)
                cat_name = coll_map.get(anc_key, anc_key)
                seen_subs[sub_name] = None
                seen_cats[cat_name] = None
            category = ", ".join(seen_cats)
            subcollection = ", ".join(seen_subs)

            result.append({
                "pmid": pmid,
                "doi": doi,
                "zotero_key": zot_key,
                "title": data.get("title", ""),
                "authors": authors,
                "year": year,
                "journal": data.get("publicationTitle", ""),
                "abstract": data.get("abstractNote", ""),
                "source_tags": source_tags,
                "category": category,
                "subcollection": subcollection,
            })

        logger.info(f"Retrieved {len(result)} library items with identifiers")
        return result

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
        stats: dict = {
            "added": 0,
            "failed": 0,
            "skipped_no_data": 0,
            "pmid_to_key": {},
            "added_indices": set(),
        }

        items_to_create = []
        ordered_pmids: list[str] = []  # parallel to items_to_create for key mapping
        ordered_orig_idx: list[int] = []  # original index in `records` per item
        for orig_i, r in enumerate(records):
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
            ordered_orig_idx.append(orig_i)

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

                    # Capture successfully created items: {pmid: key} plus the
                    # set of original-record indices that actually uploaded
                    # (covers DOI-only records that have no pmid).
                    for idx_str, item_data in resp.get("successful", {}).items():
                        zot_key = item_data.get("data", {}).get("key", "")
                        batch_pos = i + int(idx_str)
                        pmid = ordered_pmids[batch_pos]
                        if zot_key and pmid:
                            stats["pmid_to_key"][pmid] = zot_key
                        stats["added_indices"].add(ordered_orig_idx[batch_pos])

                    if n_failed > 0:
                        for idx, err in resp["failed"].items():
                            logger.warning(f"  Failed item {idx}: {err}")
                    break

                except httpx.ConnectTimeout as e:
                    # Connection never established -> the request did not reach
                    # the server, so re-sending cannot create duplicates.
                    if attempt < 2:
                        wait = 5 * (attempt + 1)
                        logger.warning(
                            f"Batch upload connect timeout (attempt {attempt + 1}/3): {e}. "
                            f"Retrying in {wait}s..."
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            f"Batch upload failed after connect-timeout retries: "
                            f"{len(batch)} item(s) not uploaded this run."
                        )
                        stats["failed"] += len(batch)
                        break

                except (httpx.ReadTimeout, httpx.TimeoutException) as e:
                    # Timed out after the request was sent: Zotero may have
                    # created the items server-side, so retrying risks
                    # duplicates. Do not retry -- defer to the next run, whose
                    # dedup baseline picks up any that landed; verify_upload
                    # reports the gap.
                    logger.warning(
                        f"Batch upload timed out after sending (attempt {attempt + 1}): {e}. "
                        f"Not retrying to avoid duplicates; {len(batch)} item(s) "
                        f"deferred to the next run."
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
    # Post-upload verification
    # ------------------------------------------------------------------

    def verify_upload(
        self,
        collection_key: str,
        expected_pmids: set[str],
        label: str = "",
    ) -> set[str]:
        """Re-fetch collection PMIDs and compare against intended uploads.

        Catches papers silently lost to Zotero timeouts or transient errors.
        Returns the set of PMIDs that were expected but not found in the
        collection after upload.
        """
        if not expected_pmids:
            return set()

        actual_pmids = set(self.get_collection_pmids(collection_key))
        missing = expected_pmids - actual_pmids

        prefix = f"{label}: " if label else ""
        if missing:
            logger.warning(
                f"{prefix}Post-upload verification: {len(missing)} of "
                f"{len(expected_pmids)} uploaded PMIDs not found in collection. "
                f"Missing: {sorted(missing)}"
            )
        else:
            logger.info(
                f"{prefix}Post-upload verification OK: all "
                f"{len(expected_pmids)} uploaded PMIDs confirmed in collection"
            )

        return missing

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    def add_relations_batch(self, edges: dict[str, set[str]]) -> None:
        """Add bidirectional dc:relation links for many items at once.

        ``edges`` maps an item key to the set of related item keys. Links are
        made symmetric, then each affected item is fetched and patched at most
        once (existing relations preserved), throttled by ``self.delay``. This
        replaces the old per-edge add_relations, which re-fetched the same
        items repeatedly and applied no rate limiting.
        """
        base_uri = f"http://zotero.org/groups/{self.zot.library_id}/items"

        # Symmetric adjacency: if A links B, B also links A.
        adj: dict[str, set[str]] = {}
        for key, related in edges.items():
            adj.setdefault(key, set()).update(related)
            for rk in related:
                adj.setdefault(rk, set()).add(key)

        for key, related in adj.items():
            add_uris = {f"{base_uri}/{k}" for k in related if k != key}
            if not add_uris:
                continue
            try:
                item = self.zot.item(key)
            except Exception as e:
                logger.warning(f"add_relations_batch: could not fetch item {key}: {e}")
                continue
            existing = item["data"].get("relations", {}).get("dc:relation", [])
            if isinstance(existing, str):
                existing = [existing]
            merged = set(existing) | add_uris
            if merged == set(existing):
                continue  # nothing new
            item["data"].setdefault("relations", {})["dc:relation"] = sorted(merged)
            try:
                self.zot.update_item(item)
            except Exception as e:
                logger.warning(f"add_relations_batch: could not patch item {key}: {e}")
            time.sleep(self.delay)
