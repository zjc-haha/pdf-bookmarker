from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookmarker import key_cache


@unittest.skipUnless(sys.platform == "win32", "Windows DPAPI is required")
class KeyCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": self.directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.path = Path(self.directory.name) / "PDFBookmarker" / "deepseek-api-key.dpapi"

    def test_missing_key_is_empty(self) -> None:
        self.assertEqual(key_cache.load_key(), "")

    def test_round_trip_stores_only_encrypted_bytes(self) -> None:
        sample_key = "sk-test-only-secret-value"
        key_cache.save_key(sample_key)

        stored = self.path.read_bytes()
        self.assertTrue(stored)
        self.assertNotIn(sample_key.encode("utf-8"), stored)
        self.assertEqual(key_cache.load_key(), sample_key)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_replacing_and_clearing_key(self) -> None:
        key_cache.save_key("first-test-key")
        key_cache.save_key("  second-test-key  ")
        self.assertEqual(key_cache.load_key(), "second-test-key")

        key_cache.save_key("  ")
        self.assertEqual(key_cache.load_key(), "")
        self.assertFalse(self.path.exists())

    def test_failed_replacement_keeps_previous_key_and_cleans_temp_file(self) -> None:
        key_cache.save_key("first-test-key")
        with patch("bookmarker.key_cache.os.replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                key_cache.save_key("second-test-key")

        self.assertEqual(key_cache.load_key(), "first-test-key")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corrupt_cache_is_reported(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"not-a-dpapi-blob")
        with self.assertRaises(key_cache.KeyCacheError):
            key_cache.load_key()


if __name__ == "__main__":
    unittest.main()
