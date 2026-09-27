"""Checks for page-offset and outline helpers shared with DeepSeek."""

from __future__ import annotations

import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from bookmarker import pipeline
from bookmarker.pipeline import (Anchor, _fit_offsets, _outline_action,
                                 _outline_page_label_ratio, _outline_quality,
                                 _title_anchors)
from bookmarker.toc import TocEntry


class PipelineTest(unittest.TestCase):
    @staticmethod
    def _source_and_entry(path: Path) -> TocEntry:
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=400)
        with path.open("wb") as stream:
            writer.write(stream)
        return TocEntry("第一章", 1, "arabic", 1, 1, "第一章 1", 1.0, pdf_page=1)

    def test_pdf_staging_stays_in_program_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            books = root / "books"
            books.mkdir()
            source = books / "original.pdf"
            entry = self._source_and_entry(source)
            destination = root / "output" / "bookmarked.pdf"
            app_data = root / "installation" / "data"
            real_reader = pipeline.PdfReader
            staged: list[Path] = []

            def inspect_reader(path: Path, *args: object, **kwargs: object) -> PdfReader:
                if Path(path).suffix == ".partial":
                    staged.append(Path(path))
                    self.assertIn(app_data / "temp", Path(path).parents)
                return real_reader(path, *args, **kwargs)

            with patch.object(pipeline.storage, "data_root", return_value=app_data), \
                 patch.object(pipeline, "PdfReader", side_effect=inspect_reader):
                pipeline._write_pdf(source, destination, [entry])
            self.assertEqual(len(staged), 1)
            self.assertTrue(destination.is_file())
            self.assertEqual([path.name for path in books.iterdir()], ["original.pdf"])
            self.assertEqual([path.name for path in destination.parent.iterdir()], ["bookmarked.pdf"])
            self.assertEqual(list((app_data / "temp").iterdir()), [])

    def test_same_volume_overwrite_replaces_original_without_neighbor_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.pdf"
            entry = self._source_and_entry(source)
            app_data = root / "installation" / "data"
            with patch.object(pipeline.storage, "data_root", return_value=app_data):
                pipeline._write_pdf(source, source, [entry])
            self.assertEqual(str(PdfReader(source).outline[0]["/Title"]), "第一章")
            self.assertEqual([path.name for path in root.iterdir() if path.is_file()], ["original.pdf"])
            self.assertEqual(list((app_data / "temp").iterdir()), [])

    def test_cross_volume_new_output_copies_and_verifies_without_user_side_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.pdf"
            entry = self._source_and_entry(source)
            destination = root / "output" / "bookmarked.pdf"
            app_data = root / "installation" / "data"
            with patch.object(pipeline.storage, "data_root", return_value=app_data), \
                 patch.object(Path, "replace", side_effect=OSError(errno.EXDEV, "other volume")):
                pipeline._write_pdf(source, destination, [entry])
            self.assertEqual(str(PdfReader(destination).outline[0]["/Title"]), "第一章")
            self.assertEqual([path.name for path in destination.parent.iterdir()], ["bookmarked.pdf"])
            self.assertEqual(list((app_data / "temp").iterdir()), [])

    def test_cross_volume_overwrite_succeeds_with_recovery_data_in_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.pdf"
            entry = self._source_and_entry(source)
            app_data = root / "installation" / "data"
            with patch.object(pipeline.storage, "data_root", return_value=app_data), \
                 patch.object(Path, "replace", side_effect=OSError(errno.EXDEV, "other volume")):
                pipeline._write_pdf(source, source, [entry])
            self.assertEqual(str(PdfReader(source).outline[0]["/Title"]), "第一章")
            self.assertEqual([path.name for path in root.iterdir() if path.is_file()], ["original.pdf"])
            self.assertEqual({item.name for item in (app_data / "temp").iterdir()},
                             {"recovery.lock"})

    def test_cross_volume_failed_overwrite_restores_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.pdf"
            entry = self._source_and_entry(source)
            original = source.read_bytes()
            app_data = root / "installation" / "data"
            copy = pipeline._copy_and_sync

            def break_write(from_path: Path, to_path: Path, *, mode: str) -> None:
                if from_path.suffix == ".partial" and mode == "r+b":
                    with to_path.open("r+b") as stream:
                        stream.write(b"broken")
                    raise OSError("interrupted write")
                copy(from_path, to_path, mode=mode)

            with patch.object(pipeline.storage, "data_root", return_value=app_data), \
                 patch.object(Path, "replace", side_effect=OSError(errno.EXDEV, "other volume")), \
                 patch.object(pipeline, "_copy_and_sync", side_effect=break_write):
                with self.assertRaisesRegex(OSError, "interrupted write"):
                    pipeline._write_pdf(source, source, [entry])
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual({item.name for item in (app_data / "temp").iterdir()},
                             {"recovery.lock"})

    def test_pending_recovery_restores_interrupted_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.pdf"
            self._source_and_entry(source)
            original = source.read_bytes()
            staged = root / "staged.pdf"
            staged.write_bytes(b"new draft")
            app_data = root / "installation" / "data"
            with patch.object(pipeline.storage, "data_root", return_value=app_data):
                recovery, _, _ = pipeline._prepare_recovery(source, staged)
                source.write_bytes(b"incomplete PDF")
                self.assertEqual(pipeline.recover_pending_overwrites(), [source])
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse(recovery.exists())

    def test_pending_recovery_keeps_fully_written_new_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.pdf"
            self._source_and_entry(source)
            staged = root / "staged.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=300, height=400)
            writer.add_outline_item("新书签", 0)
            with staged.open("wb") as stream:
                writer.write(stream)
            app_data = root / "installation" / "data"
            with patch.object(pipeline.storage, "data_root", return_value=app_data):
                recovery, _, _ = pipeline._prepare_recovery(source, staged)
                source.write_bytes(staged.read_bytes())
                self.assertEqual(pipeline.recover_pending_overwrites(), [])
                self.assertEqual(str(PdfReader(source).outline[0]["/Title"]), "新书签")
                self.assertFalse(recovery.exists())

    def test_recovery_does_not_run_while_a_writer_holds_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app_data = Path(temporary) / "installation" / "data"
            with patch.object(pipeline.storage, "data_root", return_value=app_data):
                with pipeline._recovery_lock():
                    with self.assertRaisesRegex(RuntimeError, "另一进程正在写入或恢复"):
                        pipeline.recover_pending_overwrites()

    def test_pending_recovery_keeps_different_valid_pdf_for_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.pdf"
            self._source_and_entry(source)
            staged = root / "staged.pdf"
            staged.write_bytes(b"new draft")
            replacement = root / "replacement.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=300, height=400)
            writer.add_blank_page(width=300, height=400)
            with replacement.open("wb") as stream:
                writer.write(stream)
            replacement_bytes = replacement.read_bytes()
            app_data = root / "installation" / "data"
            with patch.object(pipeline.storage, "data_root", return_value=app_data):
                recovery, _, _ = pipeline._prepare_recovery(source, staged)
                source.write_bytes(replacement_bytes)
                with self.assertRaisesRegex(RuntimeError, "另一份有效 PDF"):
                    pipeline.recover_pending_overwrites()
                self.assertEqual(source.read_bytes(), replacement_bytes)
                self.assertTrue((recovery / "backup.pdf").is_file())

    def test_cross_volume_failed_copy_removes_incomplete_new_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.pdf"
            entry = self._source_and_entry(source)
            destination = root / "output" / "bookmarked.pdf"
            app_data = root / "installation" / "data"
            with patch.object(pipeline.storage, "data_root", return_value=app_data), \
                 patch.object(Path, "replace", side_effect=OSError(errno.EXDEV, "other volume")), \
                 patch.object(pipeline.shutil, "copyfileobj", side_effect=OSError("copy failed")):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    pipeline._write_pdf(source, destination, [entry])
            self.assertFalse(destination.exists())
            self.assertFalse(destination.parent.exists())
            self.assertTrue(source.exists())
            self.assertEqual(list((app_data / "temp").iterdir()), [])

    def test_failed_publish_removes_only_new_empty_output_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.pdf"
            entry = self._source_and_entry(source)
            app_data = root / "installation" / "data"
            output = root / "existing-output"
            output.mkdir()
            marker = output / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            destination = output / "new-subfolder" / "deeper" / "bookmarked.pdf"
            with patch.object(pipeline.storage, "data_root", return_value=app_data), \
                 patch.object(Path, "replace", side_effect=PermissionError("publish failed")):
                with self.assertRaisesRegex(PermissionError, "publish failed"):
                    pipeline._write_pdf(source, destination, [entry])
            self.assertTrue(output.is_dir())
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertFalse((output / "new-subfolder").exists())
            self.assertEqual(list((app_data / "temp").iterdir()), [])

    def test_failed_publish_keeps_preexisting_empty_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.pdf"
            entry = self._source_and_entry(source)
            app_data = root / "installation" / "data"
            output = root / "existing-output"
            output.mkdir()
            destination = output / "bookmarked.pdf"
            with patch.object(pipeline.storage, "data_root", return_value=app_data), \
                 patch.object(Path, "replace", side_effect=PermissionError("publish failed")):
                with self.assertRaisesRegex(PermissionError, "publish failed"):
                    pipeline._write_pdf(source, destination, [entry])
            self.assertTrue(output.is_dir())
            self.assertEqual(list(output.iterdir()), [])

    def test_title_anchor_reads_searchable_pdf_without_external_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "title-anchor.pdf"
            book = canvas.Canvas(str(source), pagesize=(612, 792))
            book.drawString(72, 700, "Chapter 2 Advanced Methods")
            book.showPage()
            book.drawString(72, 700, "Unrelated body text")
            book.showPage()
            book.drawString(72, 700, "Chapter 2 Advanced Methods")
            book.save()
            entry = TocEntry("Chapter 2 Advanced Methods", 10, "arabic", 1, 1,
                             "Chapter 2 Advanced Methods", 1.0)

            anchors = _title_anchors(source, [1], [entry])

        self.assertEqual([(item.pdf_page, item.printed_page, item.offset)
                          for item in anchors], [(3, 10, -7)])

    def test_page_label_outline_is_replaced_but_short_chinese_title_is_useful(self) -> None:
        titles = ["封面", "书名", "版权", "前言", "目录"] + [str(i) for i in range(1, 182)]
        items = [{"/Title": title} for title in titles]
        quality = _outline_quality(items)
        self.assertAlmostEqual(quality, 5 / 186)
        self.assertAlmostEqual(_outline_page_label_ratio(items), 181 / 186)
        self.assertEqual(_outline_action(len(items), 191, quality), "replace")
        self.assertEqual(_outline_action(1, 191, _outline_quality([{"/Title": "前言"}])),
                         "preserve")
        self.assertEqual(_outline_action(40, 191, 1.0), "skip")
        self.assertEqual(_outline_action(40, 191, 1.0, skip_existing=False), "replace")

    def test_other_page_number_placeholder_formats(self) -> None:
        items = [{"/Title": title} for title in
                 ["（第 12 页）", "Page 13", "P.14", "第十五页", "一百零一", "1.2", "绪论"]]
        self.assertAlmostEqual(_outline_page_label_ratio(items), 6 / 7)
        self.assertAlmostEqual(_outline_quality(items), 1 / 7)

    def test_conflicting_labels_on_same_pages_do_not_create_segments(self) -> None:
        anchors = [Anchor(page, page - 10, "arabic", 10, "page-label")
                   for page in (12, 13, 17, 18, 91, 172)]
        anchors += [Anchor(12, 51, "arabic", -39, "page-label"),
                    Anchor(13, 52, "arabic", -39, "page-label"),
                    Anchor(17, 1, "arabic", 16, "page-label"),
                    Anchor(18, 2, "arabic", 16, "page-label")]
        offsets, segments, warnings = _fit_offsets(anchors, None, None, {"arabic"})
        self.assertEqual(offsets, {"arabic": 10})
        self.assertEqual(len(segments), 1)
        self.assertFalse(warnings)


if __name__ == "__main__":
    unittest.main()
