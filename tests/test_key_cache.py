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
        root = Path(self.directory.name)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(root / "local")})
        environment.start()
        self.addCleanup(environment.stop)
        data_patch = patch.object(key_cache, "data_root", return_value=root / "data")
        data_patch.start()
        self.addCleanup(data_patch.stop)
        self.path = root / "data" / "deepseek-api-key.dpapi"
        self.legacy_path = root / "local" / "PDFBookmarker" / "deepseek-api-key.dpapi"

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

    def test_replacing_key_and_blank_input_preserves_it(self) -> None:
        key_cache.save_key("first-test-key")
        key_cache.save_key("  second-test-key  ")
        self.assertEqual(key_cache.load_key(), "second-test-key")
        saved_bytes = self.path.read_bytes()

        key_cache.save_key("  ")
        self.assertEqual(key_cache.load_key(), "second-test-key")
        self.assertEqual(self.path.read_bytes(), saved_bytes)

    def test_blank_input_does_not_create_data_directory(self) -> None:
        key_cache.save_key("")
        self.assertFalse(self.path.parent.exists())

    def test_manual_cache_deletion_removes_saved_key(self) -> None:
        key_cache.save_key("sk-test-only-secret-value")
        self.path.unlink()
        self.assertEqual(key_cache.load_key(), "")

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
        key_cache.save_key("")
        self.assertEqual(self.path.read_bytes(), b"not-a-dpapi-blob")

    def test_legacy_key_is_migrated_and_old_empty_directory_removed(self) -> None:
        self.legacy_path.parent.mkdir(parents=True)
        encrypted = key_cache._protect(b"sk-old-secret")
        self.legacy_path.write_bytes(encrypted)

        self.assertEqual(key_cache.load_key(), "sk-old-secret")
        self.assertEqual(self.path.read_bytes(), encrypted)
        self.assertFalse(self.legacy_path.exists())
        self.assertFalse(self.legacy_path.parent.exists())

    def test_migration_keeps_old_key_when_new_write_fails(self) -> None:
        self.legacy_path.parent.mkdir(parents=True)
        encrypted = key_cache._protect(b"sk-old-secret")
        self.legacy_path.write_bytes(encrypted)

        with patch("bookmarker.key_cache.os.replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                key_cache.load_key()

        self.assertEqual(self.legacy_path.read_bytes(), encrypted)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corrupt_legacy_key_is_not_migrated(self) -> None:
        self.legacy_path.parent.mkdir(parents=True)
        self.legacy_path.write_bytes(b"not-a-dpapi-blob")

        with self.assertRaises(key_cache.KeyCacheError):
            key_cache.load_key()

        self.assertFalse(self.path.exists())
        self.assertTrue(self.legacy_path.exists())

    def test_new_key_takes_precedence_and_cleans_old_copy(self) -> None:
        key_cache.save_key("sk-new-secret")
        self.legacy_path.parent.mkdir(parents=True)
        self.legacy_path.write_bytes(key_cache._protect(b"sk-old-secret"))

        self.assertEqual(key_cache.load_key(), "sk-new-secret")
        self.assertFalse(self.legacy_path.exists())

    def test_legacy_cleanup_failure_does_not_hide_valid_local_key(self) -> None:
        key_cache.save_key("sk-new-secret")
        self.legacy_path.parent.mkdir(parents=True)
        self.legacy_path.write_bytes(key_cache._protect(b"sk-old-secret"))

        with patch.object(Path, "unlink", side_effect=PermissionError("access denied")):
            self.assertEqual(key_cache.load_key(), "sk-new-secret")
        self.assertTrue(self.legacy_path.exists())

    def test_blank_input_preserves_legacy_key_for_migration(self) -> None:
        self.legacy_path.parent.mkdir(parents=True)
        self.legacy_path.write_bytes(key_cache._protect(b"sk-old-secret"))

        key_cache.save_key("")

        self.assertTrue(self.legacy_path.exists())
        self.assertEqual(key_cache.load_key(), "sk-old-secret")
        self.assertTrue(self.path.exists())
        self.assertFalse(self.legacy_path.exists())
        self.assertFalse(self.legacy_path.parent.exists())


if __name__ == "__main__":
    unittest.main()
