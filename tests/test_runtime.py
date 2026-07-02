"""Shared entry-point bootstrap (genebot.runtime)."""

import os
import unittest
from unittest import mock

from genebot import runtime


class TestZoteroCredentials(unittest.TestCase):
    @mock.patch.dict(os.environ, {"ZOTERO_API_KEY": "k1", "ZOTERO_GROUP_ID": "424242"})
    def test_env_group_id_override(self):
        gid, key = runtime.zotero_credentials()
        self.assertEqual(gid, "424242")
        self.assertEqual(key, "k1")

    @mock.patch.dict(os.environ, {"ZOTERO_API_KEY": "k1"}, clear=False)
    def test_group_id_falls_back_to_toolkit_constant(self):
        os.environ.pop("ZOTERO_GROUP_ID", None)
        from bio_toolkit.config import ZOTERO_GROUP_ID

        gid, key = runtime.zotero_credentials()
        self.assertEqual(gid, str(ZOTERO_GROUP_ID))
        self.assertEqual(key, "k1")


class TestOpenAlexClient(unittest.TestCase):
    @mock.patch.dict(os.environ, {"OPENALEX_API_KEY": "oa"}, clear=False)
    def test_constructs_with_key(self):
        client = runtime.make_openalex_client()
        self.assertTrue(hasattr(client, "failed_request_count"))

    def test_constructs_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENALEX_API_KEY", None)
            client = runtime.make_openalex_client()
        self.assertTrue(hasattr(client, "failed_request_count"))


class TestConfigureLogging(unittest.TestCase):
    def test_stream_only_no_crash(self):
        # No stem -> stdout only, no logs/ file created.
        runtime.configure_logging()


if __name__ == "__main__":
    unittest.main()
