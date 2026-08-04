"""Pure-logic helpers in run.py (text filter, re-search dates, dedup registration)."""

import json
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


class TestGenePassSummary(unittest.TestCase):
    def test_summary_is_json_serializable(self):
        # Regression: process_gene must NOT spread **search_stats into its
        # return. add_papers() returns added_indices (a set) and pmid_to_key
        # (a dict); spreading them leaks into checkpoint["gene_summary"] and
        # crashes save_checkpoint with
        # "TypeError: Object of type set is not JSON serializable".
        search_stats = {
            "added": 2, "failed": 0, "skipped_no_data": 1,
            "pmid_to_key": {"123": "ABC"},
            "added_indices": {0, 1},
        }
        summary = run._gene_pass_summary(
            "CRB1", found=5, new=2, search_stats=search_stats,
            cit_candidates=3, cit_added=1, recent_added=0,
        )
        # Must serialize cleanly -- this is exactly what save_checkpoint does.
        json.dumps(summary)
        # And must not leak the non-scalar internals of add_papers().
        self.assertNotIn("added_indices", summary)
        self.assertNotIn("pmid_to_key", summary)
        self.assertNotIn("skipped_no_data", summary)

    def test_scalar_fields(self):
        summary = run._gene_pass_summary(
            "RHO", found=10, new=4,
            search_stats={"added": 4, "failed": 1},
            cit_candidates=7, cit_added=2, recent_added=3,
        )
        self.assertEqual(summary, {
            "symbol": "RHO", "found": 10, "new": 4,
            "added": 4, "failed": 1,
            "cit_candidates": 7, "cit_added": 2, "recent_added": 3,
        })


if __name__ == "__main__":
    unittest.main()


class TestFirstContextPair(unittest.TestCase):
    def test_pairs_positionally(self):
        self.assertEqual(
            run._first_context_pair("6 - Genes, 1 - Anatomy", "ABCA4, Outer coat"),
            ("6 - Genes", "ABCA4"),
        )
        self.assertEqual(
            run._first_context_pair("1 - Anatomy, 6 - Genes", "Outer coat, ABCA4"),
            ("1 - Anatomy", "Outer coat"),
        )

    def test_desynchronised_lengths_yield_no_target(self):
        # Legacy corrupted entry: pairing is unrecoverable, so no collection is
        # invented (which is how gene collections landed under topic categories).
        self.assertEqual(
            run._first_context_pair("1 - Anatomy, 6 - Genes", "ABCA4"),
            ("", ""),
        )

    def test_empty_fields(self):
        self.assertEqual(run._first_context_pair("", ""), ("", ""))
        self.assertEqual(run._first_context_pair("6 - Genes", ""), ("", ""))
