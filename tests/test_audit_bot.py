import json
import os
import tempfile
import unittest

import audit_bot


class TestModuleImports(unittest.TestCase):
    def test_reason_set(self):
        self.assertEqual(
            audit_bot.REASON_RESCUE_ELIGIBLE,
            {"score_below_threshold", "mention_filter"},
        )


class TestLedger(unittest.TestCase):
    def test_load_missing_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(audit_bot.load_ledger(os.path.join(d, "x.json")), set())

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "data", "audit_state.json")
            audit_bot.save_ledger(p, {"pmid:1", "pmid:2"}, now="2026-06-30T00:00:00Z")
            self.assertEqual(audit_bot.load_ledger(p), {"pmid:1", "pmid:2"})

    def test_save_writes_sorted_ids_and_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "audit_state.json")
            audit_bot.save_ledger(p, {"pmid:2", "pmid:1"}, now="2026-06-30T00:00:00Z")
            with open(p, encoding="utf-8") as f:
                payload = json.load(f)
            self.assertEqual(payload["audited_ids"], ["pmid:1", "pmid:2"])
            self.assertEqual(payload["updated_at"], "2026-06-30T00:00:00Z")

    def test_load_corrupt_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "audit_state.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(audit_bot.load_ledger(p), set())


class TestSelectFpCandidates(unittest.TestCase):
    def _lib(self):
        return [
            {"pmid": "1", "doi": "", "zotero_key": "KA", "title": "old",
             "abstract": "a", "subcollection": "ACO2", "category": "6 - Genes",
             "date_added": "2026-01-01T00:00:00Z"},
            {"pmid": "2", "doi": "", "zotero_key": "KB", "title": "new",
             "abstract": "b", "subcollection": "FBN1", "category": "6 - Genes",
             "date_added": "2026-06-01T00:00:00Z"},
        ]

    def test_excludes_audited(self):
        out = audit_bot.select_fp_candidates(self._lib(), {"pmid:2"}, 10)
        self.assertEqual([c["id"] for c in out], ["pmid:1"])

    def test_orders_newest_first(self):
        out = audit_bot.select_fp_candidates(self._lib(), set(), 10)
        self.assertEqual([c["id"] for c in out], ["pmid:2", "pmid:1"])

    def test_max_items_truncates_after_ordering(self):
        out = audit_bot.select_fp_candidates(self._lib(), set(), 1)
        self.assertEqual([c["id"] for c in out], ["pmid:2"])

    def test_candidate_shape(self):
        out = audit_bot.select_fp_candidates(self._lib(), set(), 1)
        self.assertEqual(out[0], {
            "id": "pmid:2", "key": "KB", "pmid": "2", "doi": "",
            "title": "new", "abstract": "b",
            "gene_or_topic": "FBN1", "category": "6 - Genes",
        })

    def test_skips_items_without_identifier(self):
        lib = [{"pmid": "", "doi": "", "zotero_key": "", "title": "x"}]
        self.assertEqual(audit_bot.select_fp_candidates(lib, set(), 10), [])


class TestSelectFnCandidates(unittest.TestCase):
    def _nm(self):
        return [
            {"pmid": "10", "doi": "", "title": "close", "abstract": "x",
             "subcollection": "CRB1", "category": "6 - Genes",
             "reason": "score_below_threshold", "effective_score": 3,
             "threshold": 4, "search_keywords": ["CRB1"]},
            {"pmid": "11", "doi": "", "title": "far", "abstract": "y",
             "subcollection": "RHO", "category": "6 - Genes",
             "reason": "mention_filter", "effective_score": 1,
             "threshold": 4, "search_keywords": ["RHO"]},
            {"pmid": "12", "doi": "", "title": "cancer", "abstract": "z",
             "subcollection": "RHO", "category": "6 - Genes",
             "reason": "text_exclusion", "effective_score": 5,
             "threshold": 4, "search_keywords": ["RHO"]},
        ]

    def test_excludes_text_and_mesh_exclusions(self):
        out = audit_bot.select_fn_candidates(self._nm(), set(), set(), set(), set(), set(), 10)
        self.assertNotIn("pmid:12", [c["id"] for c in out])

    def test_orders_closest_to_threshold_first(self):
        out = audit_bot.select_fn_candidates(self._nm(), set(), set(), set(), set(), set(), 10)
        self.assertEqual([c["id"] for c in out], ["pmid:10", "pmid:11"])

    def test_excludes_already_in_library(self):
        out = audit_bot.select_fn_candidates(self._nm(), set(), {"10"}, set(), set(), set(), 10)
        self.assertEqual([c["id"] for c in out], ["pmid:11"])

    def test_excludes_trashed(self):
        out = audit_bot.select_fn_candidates(self._nm(), set(), set(), set(), {"10"}, set(), 10)
        self.assertEqual([c["id"] for c in out], ["pmid:11"])

    def test_excludes_audited(self):
        out = audit_bot.select_fn_candidates(self._nm(), {"pmid:10"}, set(), set(), set(), set(), 10)
        self.assertEqual([c["id"] for c in out], ["pmid:11"])

    def test_candidate_shape_carries_context(self):
        out = audit_bot.select_fn_candidates(self._nm()[:1], set(), set(), set(), set(), set(), 1)
        self.assertEqual(out[0], {
            "id": "pmid:10", "pmid": "10", "doi": "", "title": "close",
            "abstract": "x", "gene_or_topic": "CRB1", "category": "6 - Genes",
            "reason": "score_below_threshold", "effective_score": 3,
            "threshold": 4, "search_keywords": ["CRB1"],
        })


class TestComputeApply(unittest.TestCase):
    def setUp(self):
        self.fp = [{"id": "pmid:1", "key": "K1"}, {"id": "pmid:2", "key": "K2"},
                   {"id": "pmid:3", "key": "K3"}, {"id": "pmid:4", "key": "K4"}]
        self.fn = [{"id": "pmid:5", "pmid": "5"}, {"id": "pmid:6", "pmid": "6"},
                   {"id": "pmid:7", "pmid": "7"}]

    def test_trash_requires_both_off_topic(self):
        screen = {"pmid:1": "off_topic", "pmid:2": "off_topic",
                  "pmid:3": "uncertain", "pmid:4": "on_topic"}
        adj = {"pmid:1": "off_topic", "pmid:2": "on_topic", "pmid:3": "off_topic"}
        out = audit_bot.compute_apply(self.fp, [], screen, adj)
        # pmid:1 concurs -> trash; pmid:2 Sonnet rescued it -> keep;
        # pmid:3 screener only "uncertain" (one vote) -> keep; pmid:4 on_topic -> keep
        self.assertEqual(out["to_trash_keys"], ["K1"])
        self.assertEqual(out["judged_ids"], {"pmid:1", "pmid:2", "pmid:3", "pmid:4"})

    def test_rescue_on_single_sonnet_relevant(self):
        screen = {"pmid:5": "relevant", "pmid:6": "uncertain", "pmid:7": "correctly_rejected"}
        adj = {"pmid:5": "relevant", "pmid:6": "correctly_rejected"}
        out = audit_bot.compute_apply([], self.fn, screen, adj)
        self.assertEqual([c["id"] for c in out["to_rescue"]], ["pmid:5"])
        self.assertEqual(out["judged_ids"], {"pmid:5", "pmid:6", "pmid:7"})

    def test_missing_screen_verdict_is_not_judged(self):
        out = audit_bot.compute_apply(self.fp[:1], [], {}, {})
        self.assertEqual(out["to_trash_keys"], [])
        self.assertEqual(out["judged_ids"], set())

    def test_missing_adjudication_is_not_judged(self):
        # screener flagged off_topic but no adjudicator verdict -> retry next run
        out = audit_bot.compute_apply(self.fp[:1], [], {"pmid:1": "off_topic"}, {})
        self.assertEqual(out["to_trash_keys"], [])
        self.assertEqual(out["judged_ids"], set())


class TestBuildRescueEntries(unittest.TestCase):
    def test_maps_to_rescue_queue_shape(self):
        fn = [{"id": "pmid:5", "pmid": "5", "doi": "10.1/x", "title": "T",
               "gene_or_topic": "CRB1, RHO", "category": "6 - Genes",
               "abstract": "a", "reason": "mention_filter"}]
        self.assertEqual(audit_bot.build_rescue_entries(fn), [{
            "pmid": "5", "doi": "10.1/x", "subcollection": "CRB1, RHO",
            "category": "6 - Genes", "title": "T",
        }])


class TestBatchIO(unittest.TestCase):
    def test_chunk(self):
        self.assertEqual(audit_bot.chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_write_then_load_batches_roundtrip(self):
        fp = [{"id": "pmid:1", "key": "K1"}, {"id": "pmid:2", "key": "K2"}]
        fn = [{"id": "pmid:5"}]
        with tempfile.TemporaryDirectory() as d:
            manifest = audit_bot.write_batches(d, fp, fn, batch_size=1)
            self.assertEqual(manifest["fp_batches"], ["fp_000", "fp_001"])
            self.assertEqual(manifest["fn_batches"], ["fn_000"])
            lfp, lfn = audit_bot.load_batch_items(d)
            self.assertEqual([c["id"] for c in lfp], ["pmid:1", "pmid:2"])
            self.assertEqual([c["id"] for c in lfn], ["pmid:5"])

    def test_load_verdicts_merges_files(self):
        with tempfile.TemporaryDirectory() as d:
            vdir = os.path.join(d, "verdicts")
            os.makedirs(vdir)
            with open(os.path.join(vdir, "screen_fp_000.json"), "w", encoding="utf-8") as f:
                json.dump([{"id": "pmid:1", "verdict": "off_topic", "confidence": 0.9}], f)
            with open(os.path.join(vdir, "screen_fn_000.json"), "w", encoding="utf-8") as f:
                json.dump([{"id": "pmid:5", "verdict": "relevant"}], f)
            v = audit_bot.load_verdicts(d, "screen_")
            self.assertEqual(v, {"pmid:1": "off_topic", "pmid:5": "relevant"})

    def test_select_for_adjudication(self):
        fp = [{"id": "pmid:1"}, {"id": "pmid:2"}, {"id": "pmid:3"}]
        fn = [{"id": "pmid:5"}, {"id": "pmid:6"}]
        screen = {"pmid:1": "off_topic", "pmid:2": "on_topic", "pmid:3": "uncertain",
                  "pmid:5": "relevant", "pmid:6": "correctly_rejected"}
        out = audit_bot.select_for_adjudication(fp, fn, screen)
        self.assertEqual([c["id"] for c in out], ["pmid:1", "pmid:3", "pmid:5"])


class TestStableId(unittest.TestCase):
    def test_prefers_pmid(self):
        rec = {"pmid": "123", "doi": "10.1/AbC", "zotero_key": "K"}
        self.assertEqual(audit_bot.stable_id(rec), "pmid:123")

    def test_falls_back_to_lowercased_doi(self):
        rec = {"pmid": "", "doi": "10.1/AbC", "zotero_key": "K"}
        self.assertEqual(audit_bot.stable_id(rec), "doi:10.1/abc")

    def test_falls_back_to_key(self):
        rec = {"pmid": "", "doi": "", "key": "ZK9"}
        self.assertEqual(audit_bot.stable_id(rec), "key:ZK9")

    def test_empty_when_no_identifiers(self):
        self.assertEqual(audit_bot.stable_id({}), "")


if __name__ == "__main__":
    unittest.main()
