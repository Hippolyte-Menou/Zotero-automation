"""The shared discovery-pass screening step (run.screen_works).

screen_works is the deduplicated core of what process_gene / process_topic_subtopic
did seven times inline: preserve refs -> to record -> text filter -> library dedup.
OpenAlexClient's static helpers are faked so the test exercises the real
orchestration without depending on the toolkit's work schema.
"""

import unittest
from unittest import mock

import run


class _FakeOA:
    """Stand-in for OpenAlexClient: pmid/record derived trivially from the work."""

    @staticmethod
    def extract_pmid(w):
        return w.get("pmid", "")

    @staticmethod
    def work_to_record(w):
        return {
            "pmid": w.get("pmid", ""),
            "doi": w.get("doi", ""),
            "title": w.get("title", ""),
            "abstract": w.get("abstract", ""),
        }


def _work(pmid="", doi="", title="", abstract="", refs=None):
    return {
        "pmid": pmid,
        "doi": doi,
        "title": title,
        "abstract": abstract,
        "referenced_works": refs or [],
    }


class TestScreenWorks(unittest.TestCase):
    def _screen(self, works, *, text_excl=None, existing_pmids=None, existing_dois=None):
        refs_out = {}
        with mock.patch.object(run, "OpenAlexClient", _FakeOA):
            out = run.screen_works(
                works,
                text_excl=text_excl or [],
                existing_pmids=existing_pmids or set(),
                existing_dois=existing_dois or set(),
                rejection_log=None,
                refs_out=refs_out,
            )
        return out, refs_out

    def test_preserves_referenced_works_by_pmid(self):
        works = [_work(pmid="1", title="a", refs=["W9", "W8"])]
        _, refs = self._screen(works)
        self.assertEqual(refs, {"1": ["W9", "W8"]})

    def test_refs_setdefault_first_wins(self):
        # Two works with the same pmid: the first's refs are kept.
        works = [_work(pmid="1", refs=["A"]), _work(pmid="1", refs=["B"])]
        _, refs = self._screen(works)
        self.assertEqual(refs, {"1": ["A"]})

    def test_text_filter_excludes_and_dedup_drops_existing(self):
        works = [
            _work(pmid="1", title="clean retina paper"),
            _work(pmid="2", title="a cancer paper"),        # text-excluded
            _work(pmid="3", title="already here"),          # dedup by pmid
            _work(pmid="4", doi="10.1/x", title="dup doi"),  # dedup by doi
        ]
        out, _ = self._screen(
            works,
            text_excl=["cancer"],
            existing_pmids={"3"},
            existing_dois={"10.1/x"},
        )
        self.assertEqual([r["pmid"] for r in out], ["1"])

    def test_records_without_pmid_are_dropped(self):
        works = [_work(pmid="", title="no id"), _work(pmid="7", title="ok")]
        out, _ = self._screen(works)
        self.assertEqual([r["pmid"] for r in out], ["7"])

    def test_empty_text_excl_is_passthrough(self):
        works = [_work(pmid="1", title="cancer"), _work(pmid="2", title="x")]
        out, _ = self._screen(works, text_excl=[])
        self.assertEqual({r["pmid"] for r in out}, {"1", "2"})


if __name__ == "__main__":
    unittest.main()
