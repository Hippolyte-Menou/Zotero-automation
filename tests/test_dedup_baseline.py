"""DedupBaseline value object + get_dedup_baseline() bundling (zotero_client)."""

import unittest

from genebot.zotero_client import DedupBaseline, ZoteroGroupClient


class TestDedupBaseline(unittest.TestCase):
    def test_existing_merges_active_and_trashed(self):
        b = DedupBaseline(
            pmid_to_key={"1": "KA", "2": "KB"},
            doi_to_key={"10.1/a": "KA"},
            trashed_pmids=frozenset({"9"}),
            trashed_dois=frozenset({"10.9/z"}),
        )
        self.assertEqual(b.existing_pmids, {"1", "2", "9"})
        self.assertEqual(b.existing_dois, {"10.1/a", "10.9/z"})

    def test_defaults_active_only(self):
        b = DedupBaseline(pmid_to_key={"1": "KA"}, doi_to_key={})
        self.assertEqual(b.existing_pmids, {"1"})
        self.assertEqual(b.existing_dois, set())


class _FakeZot:
    """Minimal ZoteroGroupClient stand-in exercising the four fetch calls."""

    def __init__(self):
        self.calls = []

    def get_existing_items(self):
        self.calls.append("items")
        return {"1": "KA", "2": "KB"}

    def get_existing_dois(self):
        self.calls.append("dois")
        return {"10.1/a": "KA"}

    def get_trashed_pmids(self):
        self.calls.append("tpmids")
        return {"9"}

    def get_trashed_dois(self):
        self.calls.append("tdois")
        return {"10.9/Z"}  # mixed case -> baseline lowercases


class TestGetDedupBaseline(unittest.TestCase):
    def _run(self, **kw):
        # Bind the real method to the fake so ordering logic is exercised.
        fake = _FakeZot()
        return ZoteroGroupClient.get_dedup_baseline(fake, **kw), fake

    def test_bundles_all_and_lowercases_trashed_dois(self):
        b, fake = self._run()
        self.assertEqual(b.pmid_to_key, {"1": "KA", "2": "KB"})
        self.assertEqual(b.existing_pmids, {"1", "2", "9"})
        self.assertEqual(b.existing_dois, {"10.1/a", "10.9/z"})
        self.assertEqual(fake.calls, ["items", "dois", "tpmids", "tdois"])

    def test_include_trashed_false_skips_trash(self):
        b, fake = self._run(include_trashed=False)
        self.assertEqual(b.existing_pmids, {"1", "2"})
        self.assertEqual(b.existing_dois, {"10.1/a"})
        self.assertEqual(fake.calls, ["items", "dois"])


if __name__ == "__main__":
    unittest.main()
