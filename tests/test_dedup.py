"""dedup_library clustering, canonical selection, and safe metadata merge."""

import unittest

import dedup_library as dl


def _item(key, doi="", pmid="", abstract="", title="T", tags=None, colls=None):
    data = {"key": key, "title": title}
    if doi:
        data["DOI"] = doi
    if pmid:
        data["extra"] = f"PMID: {pmid}"
    if abstract:
        data["abstractNote"] = abstract
    data["tags"] = [{"tag": t} for t in (tags or [])]
    data["collections"] = colls or []
    return {"data": data}


class TestScoreCanonical(unittest.TestCase):
    def test_more_metadata_wins(self):
        rich = _item("A", doi="10.1234/x", pmid="1", abstract="real abstract",
                     tags=["t1"], colls=["c1"])
        bare = _item("B")
        self.assertGreater(dl._score(rich), dl._score(bare))
        self.assertIs(dl._pick_canonical([bare, rich]), rich)


class TestCluster(unittest.TestCase):
    def test_shared_doi(self):
        a = _item("A", doi="10.1234/x")
        b = _item("B", doi="10.1234/x")
        c = _item("C", doi="10.5678/y")
        clusters = dl._cluster_items([a, b, c])
        self.assertEqual(sorted(len(v) for v in clusters.values()), [2])

    def test_pmid_doi_transitive(self):
        a = _item("A", doi="10.1234/x")
        b = _item("B", doi="10.1234/x", pmid="9")
        c = _item("C", pmid="9")
        clusters = dl._cluster_items([a, b, c])
        members = [sorted(i["data"]["key"] for i in v) for v in clusters.values()]
        self.assertEqual(members, [["A", "B", "C"]])


class TestMergeMetadata(unittest.TestCase):
    def test_substantial_abstract_not_overwritten(self):
        canon = _item("A", abstract="x" * 250)
        dup = _item("B", abstract="y" * 400)
        dl._merge_metadata(canon, [dup])
        self.assertEqual(canon["data"]["abstractNote"], "x" * 250)

    def test_stub_abstract_filled(self):
        canon = _item("A", abstract="short")
        dup = _item("B", abstract="y" * 300)
        dl._merge_metadata(canon, [dup])
        self.assertEqual(canon["data"]["abstractNote"], "y" * 300)

    def test_all_relation_predicates_merged(self):
        canon = _item("A")
        canon["data"]["relations"] = {}
        dup = _item("B")
        dup["data"]["relations"] = {
            "owl:sameAs": ["http://zotero.org/x/1"],
            "dc:relation": ["http://zotero.org/x/2"],
        }
        dl._merge_metadata(canon, [dup])
        rel = canon["data"]["relations"]
        self.assertIn("owl:sameAs", rel)  # non-dc:relation predicate carried over
        self.assertIn("dc:relation", rel)


if __name__ == "__main__":
    unittest.main()
