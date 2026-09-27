from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from bookmarker.preview import (PreviewError, inspect_pdf, list_pdf_paths,
                                render_first_page, render_page)


def make_pdf(path: Path, *, pages: int = 3, dense_outline: bool = False,
             encrypted: bool = False) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=300)
    chapter = writer.add_outline_item("Chapter One Introduction", 0)
    writer.add_outline_item("Section 1.1 Background", min(1, pages - 1), parent=chapter)
    if dense_outline:
        for number in range(2, 30):
            writer.add_outline_item(f"Chapter {number:02d} Details", number % pages)
    if encrypted:
        writer.encrypt("secret")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        writer.write(stream)


def make_colored_pdf(path: Path) -> None:
    """Create two pages whose pixels identify which page PDFium rendered."""
    writer = PdfWriter()
    for color in (b"1 0 0", b"0 0 1"):
        page = writer.add_blank_page(width=200, height=300)
        content = DecodedStreamObject()
        content.set_data(color + b" rg 0 0 200 300 re f")
        page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as stream:
        writer.write(stream)


class PreviewTest(unittest.TestCase):
    def test_skip_bookmarked_excludes_even_one_poor_bookmark_from_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bookmarked = root / "bookmarked.pdf"
            empty = root / "empty.pdf"
            for path, add_bookmark in ((bookmarked, True), (empty, False)):
                writer = PdfWriter()
                writer.add_blank_page(width=200, height=300)
                if add_bookmark:
                    writer.add_outline_item("1", 0)
                with path.open("wb") as stream:
                    writer.write(stream)
            skipped = inspect_pdf(bookmarked, skip_bookmarked=True,
                                  replace_existing=True, verify_existing=True,
                                  include_bookmarks=False)
            self.assertFalse(skipped.eligible)
            self.assertEqual(skipped.bookmark_count, 1)
            self.assertIn("跳过", skipped.reason)
            self.assertTrue(inspect_pdf(empty, skip_bookmarked=True).eligible)

    def test_inspect_sparse_bookmarks_preserves_titles_levels_and_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            make_pdf(source)
            preview = inspect_pdf(source)
            self.assertTrue(preview.eligible)
            self.assertEqual(preview.page_count, 3)
            self.assertEqual(preview.bookmark_count, 2)
            self.assertEqual(len(preview.bookmarks), 1)
            self.assertEqual(preview.bookmarks[0].title, "Chapter One Introduction")
            self.assertEqual(preview.bookmarks[0].page_number, 1)
            self.assertEqual(preview.bookmarks[0].children[0].title,
                             "Section 1.1 Background")
            self.assertEqual(preview.bookmarks[0].children[0].page_number, 2)
            lightweight = inspect_pdf(source, include_bookmarks=False)
            self.assertEqual(lightweight.bookmark_count, preview.bookmark_count)
            self.assertEqual(lightweight.page_count, preview.page_count)
            self.assertEqual(lightweight.bookmarks, ())
            self.assertIn("保留并补充", preview.reason)

    def test_many_page_number_bookmarks_are_candidate_for_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "numbered.pdf"
            writer = PdfWriter()
            for _ in range(191):
                writer.add_blank_page(width=200, height=300)
            for number in range(1, 187):
                writer.add_outline_item(str(number), min(number, 190))
            with source.open("wb") as stream:
                writer.write(stream)

            preview = inspect_pdf(source, include_bookmarks=False)
            self.assertTrue(preview.eligible)
            self.assertEqual(preview.bookmark_count, 186)
            self.assertEqual(preview.outline_quality, 0.0)
            self.assertIn("多为页码占位项", preview.reason)
            self.assertIn("识别可信后将替换", preview.reason)
            self.assertEqual(preview.bookmarks, ())

    def test_complete_outline_matches_pipeline_skip_and_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "complete.pdf"
            make_pdf(source, pages=35, dense_outline=True)
            normal = inspect_pdf(source)
            self.assertFalse(normal.eligible)
            self.assertEqual(normal.bookmark_count, 30)
            self.assertGreaterEqual(normal.outline_quality, 0.55)
            self.assertIn("已有 30 条", normal.reason)
            replacing = inspect_pdf(source, replace_existing=True)
            self.assertTrue(replacing.eligible)
            self.assertEqual(replacing.bookmarks, normal.bookmarks)
            checking = inspect_pdf(source, verify_existing=True, include_bookmarks=False)
            self.assertTrue(checking.eligible)
            self.assertIn("待与印刷目录核对", checking.reason)
            self.assertEqual(checking.bookmarks, ())

    def test_paths_recurse_and_exclude_output_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            output = source / "results"
            make_pdf(source / "A.pdf")
            make_pdf(source / "sub" / "B.PDF")
            make_pdf(output / "generated.pdf")
            (source / "ignore.txt").write_text("not a PDF", encoding="utf-8")
            self.assertEqual(list_pdf_paths(source, output),
                             [source / "A.pdf", source / "sub" / "B.PDF"])

    def test_encrypted_and_invalid_files_report_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            encrypted = root / "locked.pdf"
            make_pdf(encrypted, encrypted=True)
            self.assertFalse(inspect_pdf(encrypted).eligible)
            self.assertIn("加密", inspect_pdf(encrypted).reason)
            with self.assertRaisesRegex(PreviewError, "加密"):
                render_first_page(encrypted)
            broken = root / "broken.pdf"
            broken.write_bytes(b"not a PDF")
            self.assertFalse(inspect_pdf(broken).eligible)
            self.assertIn("无法读取", inspect_pdf(broken).reason)

    def test_render_first_page_returns_small_detached_rgb_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            make_pdf(source)
            image = render_first_page(source, max_size=450)
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(max(image.size), 450)
            self.assertEqual(image.getpixel((image.width // 2, image.height // 2)),
                             (255, 255, 255))

    def test_render_page_selects_requested_page_and_returns_detached_rgb_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "colored.pdf"
            make_colored_pdf(source)
            first = render_first_page(source, max_size=400)
            second = render_page(source, 2, max_size=400)
            self.assertEqual(first.mode, "RGB")
            self.assertEqual(second.mode, "RGB")
            self.assertEqual(max(second.size), 400)
            self.assertEqual(first.getpixel((first.width // 2, first.height // 2)),
                             (255, 0, 0))
            self.assertEqual(second.getpixel((second.width // 2, second.height // 2)),
                             (0, 0, 255))
        # Both pixel buffers remain usable after PDFium releases the document
        # and the source PDF has been removed.
        self.assertEqual(second.getpixel((second.width // 2, second.height // 2)),
                         (0, 0, 255))

    def test_render_page_rejects_invalid_page_numbers_and_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            make_pdf(source, pages=3)
            for number in (0, 4):
                with self.subTest(number=number), self.assertRaisesRegex(ValueError, "页码超出范围"):
                    render_page(source, number)
            for number in (True, 1.5, "2"):
                with self.subTest(number=number), self.assertRaisesRegex(ValueError, "页码必须是整数"):
                    render_page(source, number)
            for size in (99, 2401, 300.5, True):
                with self.subTest(size=size), self.assertRaisesRegex(ValueError, "max_size"):
                    render_page(source, 1, max_size=size)


if __name__ == "__main__":
    unittest.main()
