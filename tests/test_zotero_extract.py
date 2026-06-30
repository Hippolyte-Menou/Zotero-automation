"""PMID / DOI extraction and collection-ancestor walking in zotero_client."""

import unittest

from genebot.zotero_client import ZoteroGroupClient as Z


class TestPmidExtract(unittest.TestCase):
    def test_prefix_variants(self):
        self.assertEqual(Z._extract_pmid_from_data({"extra": "PMID: 12345"}), "12345")
        self.assertEqual(Z._extract_pmid_from_data({"extra": "pmid:678"}), "678")
        self.assertEqual(Z._extract_pmid_from_data({"extra": "PubMed ID: 999"}), "999")

    def test_from_url(self):
        self.assertEqual(
            Z._extract_pmid_from_data({"url": "https://pubmed.ncbi.nlm.nih.gov/2222/"}),
            "2222",
        )
        self.assertEqual(
            Z._extract_pmid_from_data({"url": "https://www.ncbi.nlm.nih.gov/pubmed/3333"}),
            "3333",
        )

    def test_none(self):
        self.assertEqual(Z._extract_pmid_from_data({"extra": "no id here"}), "")
        self.assertEqual(Z._extract_pmid_from_data({}), "")


class TestDoiExtract(unittest.TestCase):
    def test_field_normalized(self):
        self.assertEqual(
            Z._extract_doi_from_data({"DOI": "https://doi.org/10.1234/AbC"}),
            "10.1234/abc",
        )

    def test_from_extra_case_insensitive(self):
        # Regression: the extra-field regex now uses re.IGNORECASE, so mixed /
        # upper case "DOI:" prefixes are recognised.
        self.assertEqual(
            Z._extract_doi_from_data({"extra": "DOI: 10.1234/xyz"}), "10.1234/xyz"
        )
        self.assertEqual(
            Z._extract_doi_from_data({"extra": "doi:10.1234/aB"}), "10.1234/ab"
        )

    def test_none(self):
        self.assertEqual(Z._extract_doi_from_data({}), "")


class TestTopLevelAncestor(unittest.TestCase):
    def test_walk_to_top(self):
        parent_map = {"C": "B", "B": "A", "A": None}
        self.assertEqual(Z._get_top_level_ancestor("C", parent_map), "A")

    def test_cycle_protection_terminates(self):
        parent_map = {"X": "Y", "Y": "X"}
        # Must not loop forever; returns the start key as a fallback.
        self.assertEqual(Z._get_top_level_ancestor("X", parent_map), "X")


if __name__ == "__main__":
    unittest.main()
