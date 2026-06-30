"""Pure-logic helpers in run.py (text filter, re-search dates, dedup registration)."""

import unittest

import run


class TestFilterRecordsByText(unittest.TestCase):
    def test_excludes_matching_term(self):
        recs = [
            {"title": "about cancer", "abstract": ""},
            {"title": "retina study", "abstract": ""},
        ]
        out = run.filter_records_by_text(recs, ["cancer"])
        self.assertEqual([r["title"] for r in out], ["retina study"])

    def test_word_boundary_no_substring_match(self):
        # \bcancer\b must not match inside "cancerous".
        recs = [{"title": "cancerous tissue", "abstract": ""}]
        out = run.filter_records_by_text(recs, ["cancer"])
        self.assertEqual(len(out), 1)

    def test_empty_terms_passthrough(self):
        recs = [{"title": "x", "abstract": ""}]
        self.assertEqual(run.filter_records_by_text(recs, []), recs)


class TestReSearchDate(unittest.TestCase):
    def test_record_sets_date(self):
        cache = {"genes": {}}
        run._record_search_date("CRB1", cache)
        self.assertIn("last_search_date", cache["genes"]["CRB1"])

    def test_rotation_write_preserves_search_date(self):
        # Regression for the H1 clobber: writing last_full_expanded must MERGE
        # into the gene entry, not replace it, so last_search_date survives.
        cache = {"genes": {}}
        run._record_search_date("CRB1", cache)
        saved = cache["genes"]["CRB1"]["last_search_date"]

        # Mirror the fixed process_gene rotation write.
        gene_entry = cache.setdefault("genes", {}).setdefault("CRB1", {})
        gene_entry["last_full_expanded"] = "2026-01-01"

        self.assertEqual(cache["genes"]["CRB1"]["last_search_date"], saved)
        self.assertEqual(cache["genes"]["CRB1"]["last_full_expanded"], "2026-01-01")


class TestRegisterNewRecords(unittest.TestCase):
    def test_added_indices_filters_failed_uploads(self):
        existing_p, existing_d, new_p = set(), set(), set()
        recs = [{"pmid": "1", "doi": "10.1234/a"}, {"pmid": "2", "doi": ""}]
        # Only the record at index 0 actually uploaded.
        run._register_new_records(recs, existing_p, existing_d, new_p, added_indices={0})
        self.assertEqual(new_p, {"1"})
        self.assertIn("1", existing_p)
        self.assertNotIn("2", existing_p)
        self.assertIn("10.1234/a", existing_d)

    def test_none_registers_all(self):
        existing_p, existing_d, new_p = set(), set(), set()
        recs = [{"pmid": "1", "doi": ""}, {"pmid": "2", "doi": ""}]
        run._register_new_records(recs, existing_p, existing_d, new_p)
        self.assertEqual(new_p, {"1", "2"})


if __name__ == "__main__":
    unittest.main()
