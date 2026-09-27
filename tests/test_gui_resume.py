from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bookmarker.gui import _resume_skips, build_batch_command, build_batch_environment
from bookmarker.deepseek import DEEPSEEK_MODEL, PROMPT_VERSION
from bookmarker.toc import HIERARCHY_VERSION


class GuiResumeTest(unittest.TestCase):
    def test_worker_command_accepts_deepseek_only(self) -> None:
        command = build_batch_command(Path("input"), Path("output"), frozen=False)
        self.assertNotIn("--engine", command)
        self.assertNotIn("--ocr", command)
        with self.assertRaises(ValueError):
            build_batch_command(Path("input"), Path("output"), engine="ocr",
                                frozen=False)
        with self.assertRaises(ValueError):
            build_batch_environment(engine="ocr")

    def test_skip_bookmarked_command_and_resume_option(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "books"
            output_dir = root / "reports"
            source_dir.mkdir()
            source = source_dir / "one.pdf"
            source.write_bytes(b"sample")
            stat = source.stat()
            old = {
                "status": "skipped",
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "options": {
                    "engine": "deepseek", "model": DEEPSEEK_MODEL,
                    "prompt_version": PROMPT_VERSION,
                    "hierarchy_version": HIERARCHY_VERSION,
                    "ocr": None, "front": 35, "back": 12,
                    "replace_existing": False, "verify_existing": False,
                    "dry_run": False, "overwrite_original": False,
                },
            }
            settings = dict(dry_run=False, replace_existing=False)
            self.assertTrue(_resume_skips(source, source_dir, output_dir, old, **settings))
            self.assertFalse(_resume_skips(source, source_dir, output_dir, old,
                                           **settings, skip_bookmarked=True))
            old["options"]["skip_bookmarked"] = True
            self.assertTrue(_resume_skips(source, source_dir, output_dir, old,
                                          **settings, skip_bookmarked=True))
            for input_path, single_file in ((source, True), (source_dir, False)):
                with self.subTest(single_file=single_file):
                    command = build_batch_command(
                        input_path, output_dir, single_file=single_file,
                        skip_bookmarked=True, frozen=False)
                    self.assertIn("--skip-bookmarked", command)
            self.assertNotIn("--skip-bookmarked", build_batch_command(
                source, output_dir, single_file=True, frozen=False))

    def test_overwrite_command_and_resume_use_original_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "books"
            output_dir = root / "reports"
            source_dir.mkdir()
            source = source_dir / "one.pdf"
            source.write_bytes(b"completed PDF")
            stat = source.stat()
            old = {
                "status": "success",
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "options": {
                    "engine": "deepseek", "model": DEEPSEEK_MODEL,
                    "prompt_version": PROMPT_VERSION,
                    "hierarchy_version": HIERARCHY_VERSION,
                    "ocr": None, "front": 35, "back": 12,
                    "replace_existing": False, "verify_existing": False,
                    "dry_run": False, "overwrite_original": True,
                },
            }
            settings = dict(dry_run=False, replace_existing=False,
                            overwrite_original=True)
            self.assertTrue(_resume_skips(source, source_dir, output_dir, old, **settings))
            self.assertFalse(_resume_skips(source, source_dir, output_dir, old,
                                           **{**settings, "overwrite_original": False}))
            for input_path, single_file in ((source, True), (source_dir, False)):
                with self.subTest(single_file=single_file):
                    command = build_batch_command(
                        input_path, output_dir, single_file=single_file,
                        overwrite_original=True, frozen=False)
                    self.assertIn("--overwrite-original", command)
                    self.assertIn("--output", command)
            self.assertNotIn("--overwrite-original", build_batch_command(
                source, output_dir, single_file=True, frozen=False))

    def test_completed_book_requires_matching_options_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "books"
            output_dir = root / "output"
            source_dir.mkdir()
            output_dir.mkdir()
            source = source_dir / "one.pdf"
            source.write_bytes(b"sample")
            stat = source.stat()
            old = {
                "status": "success",
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "options": {
                    "engine": "deepseek", "model": DEEPSEEK_MODEL,
                    "prompt_version": PROMPT_VERSION,
                    "hierarchy_version": HIERARCHY_VERSION,
                    "ocr": None, "front": 35, "back": 12,
                    "replace_existing": False, "verify_existing": False,
                    "dry_run": False,
                },
            }
            settings = dict(dry_run=False, replace_existing=False)
            self.assertFalse(_resume_skips(source, source_dir, output_dir, old, **settings))
            (output_dir / "one_deepseek_bookmarked.pdf").touch()
            self.assertTrue(_resume_skips(source, source_dir, output_dir, old, **settings))
            old["options"].pop("verify_existing")
            self.assertTrue(_resume_skips(source, source_dir, output_dir, old, **settings))
            old["options"].pop("hierarchy_version")
            self.assertFalse(_resume_skips(source, source_dir, output_dir, old, **settings))
            old["options"]["hierarchy_version"] = HIERARCHY_VERSION
            self.assertFalse(_resume_skips(
                source, source_dir, output_dir, old, **{**settings, "replace_existing": True}))
            self.assertFalse(_resume_skips(
                source, source_dir, output_dir, old, **{**settings, "verify_existing": True}))
            source.write_bytes(b"changed")
            self.assertFalse(_resume_skips(source, source_dir, output_dir, old, **settings))


if __name__ == "__main__":
    unittest.main()
