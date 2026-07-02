"""Shared JSON read/write primitives (genebot.jsonstore)."""

import json
import os
import tempfile
import unittest

from genebot import jsonstore


class TestReadJson(unittest.TestCase):
    def test_missing_returns_default(self):
        self.assertEqual(jsonstore.read_json("nope/xyz.json", {"d": 1}), {"d": 1})
        self.assertIsNone(jsonstore.read_json("nope/xyz.json"))

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            jsonstore.write_json(p, {"x": [1, 2]})
            self.assertEqual(jsonstore.read_json(p), {"x": [1, 2]})

    def test_corrupt_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(jsonstore.read_json(p, default=[]), [])


class TestWriteJson(unittest.TestCase):
    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "deep", "b.json")
            jsonstore.write_json(p, {"ok": True}, indent=1)
            self.assertTrue(os.path.isfile(p))
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"ok": True})

    def test_indent_passthrough(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            jsonstore.write_json(p, {"a": 1}, indent=2)
            with open(p, encoding="utf-8") as f:
                self.assertIn("\n", f.read())  # indented output has newlines


if __name__ == "__main__":
    unittest.main()
