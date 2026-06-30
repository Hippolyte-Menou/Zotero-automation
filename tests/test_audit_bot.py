import unittest

import audit_bot


class TestModuleImports(unittest.TestCase):
    def test_reason_set(self):
        self.assertEqual(
            audit_bot.REASON_RESCUE_ELIGIBLE,
            {"score_below_threshold", "mention_filter"},
        )


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
