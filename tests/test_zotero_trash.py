import unittest

from genebot.zotero_client import ZoteroGroupClient


class FakeZot:
    def __init__(self):
        self.deleted = []
        self.items_by_key = {"K1": {"data": {"key": "K1", "title": "one"}},
                             "K2": {"data": {"key": "K2", "title": "two"}}}

    def item(self, key):
        return self.items_by_key[key]

    def delete_item(self, item):
        self.deleted.append(item["data"]["key"])


class TestTrashItems(unittest.TestCase):
    def _client(self, fake):
        c = ZoteroGroupClient.__new__(ZoteroGroupClient)  # bypass __init__/network
        c.zot = fake
        return c

    def test_dry_run_does_not_delete(self):
        fake = FakeZot()
        result = self._client(fake).trash_items(["K1", "K2"], apply=False)
        self.assertEqual(fake.deleted, [])
        self.assertEqual(result["would_trash"], ["K1", "K2"])
        self.assertEqual(result["trashed"], 0)

    def test_apply_deletes_each_key(self):
        fake = FakeZot()
        result = self._client(fake).trash_items(["K1", "K2"], apply=True)
        self.assertEqual(fake.deleted, ["K1", "K2"])
        self.assertEqual(result["trashed"], 2)
        self.assertEqual(result["failed"], 0)

    def test_apply_counts_failures_without_raising(self):
        fake = FakeZot()
        result = self._client(fake).trash_items(["K1", "MISSING"], apply=True)
        self.assertEqual(fake.deleted, ["K1"])
        self.assertEqual(result["trashed"], 1)
        self.assertEqual(result["failed"], 1)


if __name__ == "__main__":
    unittest.main()
