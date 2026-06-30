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
