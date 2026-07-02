"""Canonical identifier extraction (genebot.identifiers)."""

import unittest

from genebot import identifiers as ids


class TestExtractPmid(unittest.TestCase):
    def test_prefix_variants(self):
        self.assertEqual(ids.extract_pmid({"extra": "PMID: 12345"}), "12345")
        self.assertEqual(ids.extract_pmid({"extra": "pmid:678"}), "678")
        self.assertEqual(ids.extract_pmid({"extra": "PubMed ID: 999"}), "999")

    def test_from_url_both_formats(self):
        self.assertEqual(
            ids.extract_pmid({"url": "https://pubmed.ncbi.nlm.nih.gov/2222/"}), "2222"
        )
        self.assertEqual(
            ids.extract_pmid({"url": "https://www.ncbi.nlm.nih.gov/pubmed/3333"}), "3333"
        )

    def test_pubmed_url_embedded_in_extra(self):
        self.assertEqual(
            ids.extract_pmid({"extra": "see https://pubmed.ncbi.nlm.nih.gov/4444/"}),
            "4444",
        )

    def test_none(self):
        self.assertEqual(ids.extract_pmid({"extra": "no id here"}), "")
        self.assertEqual(ids.extract_pmid({}), "")


class TestExtractDoi(unittest.TestCase):
    def test_field_normalized(self):
        self.assertEqual(
            ids.extract_doi({"DOI": "https://doi.org/10.1234/AbC"}), "10.1234/abc"
        )

    def test_from_extra_case_insensitive(self):
        self.assertEqual(ids.extract_doi({"extra": "DOI: 10.1234/xyz"}), "10.1234/xyz")
        self.assertEqual(ids.extract_doi({"extra": "doi:10.1234/aB"}), "10.1234/ab")

    def test_from_url_fallback(self):
        # Unified behaviour: a doi.org URL is now recognised (was pdf_helpers-only).
        self.assertEqual(
            ids.extract_doi({"url": "https://doi.org/10.5555/qq"}), "10.5555/qq"
        )

    def test_field_precedence_over_url(self):
        self.assertEqual(
            ids.extract_doi({"DOI": "10.1/a", "url": "https://doi.org/10.2/b"}),
            "10.1/a",
        )

    def test_none(self):
        self.assertEqual(ids.extract_doi({}), "")


class TestExtractPmcid(unittest.TestCase):
    def test_found(self):
        self.assertEqual(ids.extract_pmcid({"extra": "PMCID: PMC123"}), "PMC123")

    def test_none(self):
        self.assertEqual(ids.extract_pmcid({"extra": "nothing"}), "")
        self.assertEqual(ids.extract_pmcid({}), "")


if __name__ == "__main__":
    unittest.main()
