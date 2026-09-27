"""Program data paths should be predictable without creating user data."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookmarker import storage


class StorageTest(unittest.TestCase):
    def test_source_mode_uses_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            self.assertEqual(storage.data_root(frozen=False, project_root=project),
                             project / "data")
            self.assertFalse((project / "data").exists())

    def test_frozen_mode_uses_executable_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "portable" / "PDF书签工具.exe"
            self.assertEqual(storage.data_root(frozen=True, executable=executable),
                             executable.parent / "data")
            self.assertFalse((executable.parent / "data").exists())

    def test_runtime_frozen_flag_is_respected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "PDF书签工具.exe"
            with patch.object(storage.sys, "frozen", True, create=True), \
                 patch.object(storage.sys, "executable", str(executable)):
                self.assertEqual(storage.data_root(), executable.parent / "data")

    def test_private_temp_redirects_library_scratch_to_program_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "portable" / "data"
            old_tempdir = tempfile.tempdir
            try:
                with patch.object(storage, "data_root", return_value=root), \
                     patch.dict(storage.os.environ, {}, clear=False):
                    folder = storage.configure_private_temp()
                    self.assertEqual(folder, root / "temp")
                    self.assertTrue(folder.is_dir())
                    for name in ("TEMP", "TMP", "TMPDIR"):
                        self.assertEqual(storage.os.environ[name], str(folder))
                    self.assertEqual(Path(tempfile.gettempdir()), folder)
            finally:
                tempfile.tempdir = old_tempdir

    def test_job_identity_uses_input_output_and_overwrite_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = root / "books" / "a.pdf"
            other_book = root / "books" / "b.pdf"
            output_a = root / "output-a"
            output_b = root / "output-b"
            first = storage.job_data_dir(book, output_a, root=root / "data")

            self.assertEqual(first, storage.job_data_dir(book, output_a, root=root / "data"))
            self.assertEqual(first.parent, root / "data" / "jobs")
            self.assertNotEqual(first, storage.job_data_dir(book, output_b, root=root / "data"))
            self.assertNotEqual(first, storage.job_data_dir(other_book, output_a, root=root / "data"))
            self.assertNotEqual(first, storage.job_data_dir(book, output_a, True, root=root / "data"))
            self.assertEqual(storage.job_data_dir(book, output_a, True, root=root / "data"),
                             storage.job_data_dir(book, output_b, True, root=root / "data"))
            self.assertFalse((root / "data").exists())

    def test_legacy_artifacts_move_to_job_without_touching_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            pdf = output / "book_deepseek_bookmarked.pdf"
            pdf.write_bytes(b"pdf result")
            old_report = output / "bookmarker-report.jsonl"
            old_report.write_bytes(b'{"source":"book.pdf","status":"success"}\n')
            old_summary = output / "bookmarker-summary.csv"
            old_summary.write_text("source,status\nbook.pdf,success\n", encoding="utf-8-sig")
            old_cache = output / ".bookmarker-cache" / "fingerprint"
            old_cache.mkdir(parents=True)
            (old_cache / "toc.json").write_bytes(b"cached toc")
            job = storage.job_data_dir(root / "books", output, root=root / "data")

            moved = storage.migrate_legacy_job_data(output, job)

            self.assertEqual(moved, ("bookmarker-report.jsonl", "bookmarker-summary.csv",
                                     ".bookmarker-cache"))
            self.assertEqual([path.name for path in output.iterdir()], [pdf.name])
            self.assertEqual(pdf.read_bytes(), b"pdf result")
            self.assertEqual((job / "bookmarker-report.jsonl").read_bytes(),
                             b'{"source":"book.pdf","status":"success"}\n')
            self.assertIn("book.pdf,success", (job / "bookmarker-summary.csv").read_text(
                encoding="utf-8-sig"))
            self.assertEqual((job / "cache" / "fingerprint" / "toc.json").read_bytes(),
                             b"cached toc")
            self.assertEqual(storage.migrate_legacy_job_data(output, job), ())

    def test_existing_reports_merge_with_new_rows_winning_and_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            job = root / "data" / "jobs" / "job-id"
            job.mkdir(parents=True)
            old_rows = (b'{"source":"a.pdf","status":"failed"}\n'
                        b'{"source":"b.pdf","status":"success"}\n')
            (output / "bookmarker-report.jsonl").write_bytes(old_rows)
            (job / "bookmarker-report.jsonl").write_bytes(
                b'{"source":"a.pdf","status":"success"}\n')
            old_csv = b"source,status\na.pdf,failed\nb.pdf,success\n"
            (output / "bookmarker-summary.csv").write_bytes(old_csv)
            (job / "bookmarker-summary.csv").write_bytes(b"source,status\na.pdf,success\n")

            storage.migrate_legacy_job_data(output, job)
            # Simulate interruption after the merged destination was written,
            # before the old source could be deleted.
            (output / "bookmarker-report.jsonl").write_bytes(old_rows)
            (output / "bookmarker-summary.csv").write_bytes(old_csv)
            storage.migrate_legacy_job_data(output, job)

            rows = [json.loads(line) for line in (job / "bookmarker-report.jsonl").read_text(
                encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 3)
            latest = {row["source"]: row["status"] for row in rows}
            self.assertEqual(latest, {"a.pdf": "success", "b.pdf": "success"})
            summary = list(csv.DictReader(io.StringIO((job / "bookmarker-summary.csv").read_text(
                encoding="utf-8-sig"))))
            self.assertEqual({row["source"]: row["status"] for row in summary}, latest)
            self.assertEqual(list(output.iterdir()), [])

    def test_cache_conflict_is_archived_without_overwriting_new_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source_cache = output / ".bookmarker-cache" / "key"
            source_cache.mkdir(parents=True)
            (source_cache / "toc.json").write_bytes(b"old")
            job = root / "data" / "jobs" / "job-id"
            new_cache = job / "cache" / "key"
            new_cache.mkdir(parents=True)
            (new_cache / "toc.json").write_bytes(b"new")

            storage.migrate_legacy_job_data(output, job)

            self.assertEqual((new_cache / "toc.json").read_bytes(), b"new")
            archived = list((job / "legacy-cache-conflicts" / "key").iterdir())
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), b"old")
            self.assertFalse((output / ".bookmarker-cache").exists())

    def test_failed_destination_write_preserves_old_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            old = output / "bookmarker-report.jsonl"
            old.write_bytes(b'{"source":"book.pdf"}\n')
            job = root / "data" / "jobs" / "job-id"

            with patch.object(storage.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(storage.LegacyDataMigrationError):
                    storage.migrate_legacy_job_data(output, job)

            self.assertEqual(old.read_bytes(), b'{"source":"book.pdf"}\n')
            self.assertFalse((job / "bookmarker-report.jsonl").exists())
            self.assertEqual(list(job.glob("*.tmp")), [])

    def test_linked_legacy_artifact_is_refused_without_moving_anything(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            external = root / "outside.jsonl"
            external.write_bytes(b"private")
            linked = output / "bookmarker-report.jsonl"
            try:
                linked.symlink_to(external)
            except OSError:
                self.skipTest("File symlinks are unavailable")
            job = root / "data" / "jobs" / "job-id"

            with self.assertRaises(storage.LegacyDataMigrationError):
                storage.migrate_legacy_job_data(output, job)

            self.assertEqual(external.read_bytes(), b"private")
            self.assertTrue(linked.is_symlink())
            self.assertFalse(job.exists())

    def test_redirect_preflight_preserves_other_old_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            report = output / "bookmarker-report.jsonl"
            report.write_bytes(b"old report")
            summary = output / "bookmarker-summary.csv"
            summary.write_bytes(b"old summary")
            job = root / "data" / "jobs" / "job-id"
            actual_check = storage._is_redirect

            with patch.object(storage, "_is_redirect",
                              side_effect=lambda path: path == summary or actual_check(path)):
                with self.assertRaises(storage.LegacyDataMigrationError):
                    storage.migrate_legacy_job_data(output, job)

            self.assertEqual(report.read_bytes(), b"old report")
            self.assertEqual(summary.read_bytes(), b"old summary")
            self.assertFalse(job.exists())

    def test_overwrite_never_looks_at_supplied_output(self) -> None:
        class ForbiddenPath:
            def __fspath__(self) -> str:
                raise AssertionError("The output path was touched")

        self.assertEqual(storage.migrate_legacy_job_data(
            ForbiddenPath(), ForbiddenPath(), overwrite_original=True), ())


if __name__ == "__main__":
    unittest.main()
