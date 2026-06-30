"""Cumulative-merge behaviour of RejectionLog.to_json()."""

import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
