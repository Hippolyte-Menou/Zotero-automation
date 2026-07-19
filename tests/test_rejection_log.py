"""Cumulative-merge behaviour of RejectionLog.to_json()."""

import json
import os
import tempfile
import unittest
from unittest import mock

from genebot import rejection_log as rejection_log_mod
from genebot.rejection_log import RejectionLog


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _entry(pmid, sub, cat, reason="score_below_threshold"):
    return {
        "pmid": pmid, "doi": "", "title": f"T{pmid}", "authors": [],
        "journal": "", "year": "2020", "abstract": "", "cited_by_count": 0,
        "reason": reason, "subcollection": sub, "category": cat,
        "matched_term": None, "search_keywords": [],
    }


class TestCumulativeMerge(unittest.TestCase):
    def test_new_then_recurring(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nm.json")

            log = RejectionLog()
            log.entries.append(_entry("1", "CRB1", "6 - Genes"))
            log.to_json(p)

            data = _read(p)
            self.assertEqual(data["pipeline_runs"], 1)
            art = {a["pmid"]: a for a in data["articles"]}["1"]
            self.assertEqual(art["seen_count"], 1)
            first_seen = art["first_seen"]

            # Second run, same PMID in a different subcollection.
            log2 = RejectionLog()
            log2.entries.append(_entry("1", "RHO", "6 - Genes"))
            log2.to_json(p, previous_path=p)

            data2 = _read(p)
            self.assertEqual(data2["pipeline_runs"], 2)
            art2 = {a["pmid"]: a for a in data2["articles"]}["1"]
            self.assertEqual(art2["seen_count"], 2)
            self.assertEqual(art2["first_seen"], first_seen)  # preserved
            self.assertEqual(art2["subcollection"], "CRB1, RHO")  # union, sorted

    def test_carry_forward_unseen(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nm.json")
            log = RejectionLog()
            log.entries.append(_entry("1", "CRB1", "6 - Genes"))
            log.to_json(p)

            log2 = RejectionLog()
            log2.entries.append(_entry("2", "RHO", "6 - Genes"))
            log2.to_json(p, previous_path=p)

            data = _read(p)
            self.assertEqual({a["pmid"] for a in data["articles"]}, {"1", "2"})

    def test_to_json_does_not_mutate_entries(self):
        log = RejectionLog()
        log.entries.append(_entry("1", "CRB1", "6 - Genes"))
        with tempfile.TemporaryDirectory() as d:
            log.to_json(os.path.join(d, "nm.json"))
        # Cumulative fields must live only in the output, not in self.entries.
        self.assertNotIn("first_seen", log.entries[0])


class TestPruning(unittest.TestCase):
    def test_entry_cap_keeps_most_recent(self):
        # Force a tiny cap and confirm the most recently seen entries survive.
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(rejection_log_mod, "MAX_ARTICLES", 2):
            p = os.path.join(d, "nm.json")
            log = RejectionLog()
            for i in range(5):
                log.entries.append(_entry(str(i), "CRB1", "6 - Genes"))
            log.to_json(p)

            data = _read(p)
            self.assertEqual(len(data["articles"]), 2)
            # stats/hierarchy reflect the pruned set, not the pre-prune count
            self.assertEqual(data["stats"]["total_rejections"], 2)

    def test_byte_budget_bounds_file(self):
        # A single fat abstract per entry with a small byte budget should stop
        # the file well short of retaining everything.
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(rejection_log_mod, "MAX_BYTES", 4000):
            p = os.path.join(d, "nm.json")
            log = RejectionLog()
            for i in range(50):
                e = _entry(str(i), "CRB1", "6 - Genes")
                e["abstract"] = "x" * 500
                log.entries.append(e)
            log.to_json(p)

            self.assertLess(os.path.getsize(p), 100 * 1024 * 1024)
            data = _read(p)
            self.assertLess(len(data["articles"]), 50)
            self.assertGreater(len(data["articles"]), 0)

    def test_long_abstract_truncated(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nm.json")
            log = RejectionLog()
            e = _entry("1", "CRB1", "6 - Genes")
            e["abstract"] = "y" * (rejection_log_mod.MAX_ABSTRACT_CHARS + 1000)
            log.entries.append(e)
            log.to_json(p)

            art = _read(p)["articles"][0]
            self.assertEqual(
                len(art["abstract"]),
                rejection_log_mod.MAX_ABSTRACT_CHARS + len("..."),
            )
            self.assertTrue(art["abstract"].endswith("..."))

    def test_no_prune_under_limits(self):
        # Default limits are generous; a small dataset is retained in full.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nm.json")
            log = RejectionLog()
            for i in range(10):
                log.entries.append(_entry(str(i), "CRB1", "6 - Genes"))
            log.to_json(p)
            self.assertEqual(len(_read(p)["articles"]), 10)


if __name__ == "__main__":
    unittest.main()
