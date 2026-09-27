from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from bookmarker.pipeline import _write_pdf
from bookmarker.toc import TocEntry, _page_value, normalize_levels


class TocParsingTest(unittest.TestCase):
    def test_numbered_sibling_consensus_fixes_isolated_cross_page_drift(self) -> None:
        titles_and_levels = [
            ("第1章 矩阵代数基础", 1),
            ("1.1 矩阵", 2),
            ("1.2 向量", 1),
            ("1.3 变换", 2),
            ("1.3.1 定义", 3),
            ("本章小结", 2),
            ("第2章 特殊矩阵", 1),
        ]
        entries = [TocEntry(title, index, "arabic", level, 10 if index < 3 else 11,
                            title, 1.0)
                   for index, (title, level) in enumerate(titles_and_levels, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 2, 2, 2, 3, 2, 1])

    def test_numbered_children_without_parent_do_not_nest_under_siblings(self) -> None:
        titles = ["第2章 特殊矩阵", "2.8.1 定义", "2.8.2 性质", "2.9 其它"]
        entries = [TocEntry(title, index, "arabic", 3, 1, title, 1.0)
                   for index, title in enumerate(titles, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 2, 2, 2])

    def test_numbering_does_not_override_one_flat_visual_level(self) -> None:
        titles = ["第一章 基础", "1.1 矩阵", "1.1.1 向量", "1.2 运算", "第二章 应用",
                  "2.8.1 定义", "2.8.2 性质"]
        entries = [TocEntry(title, index, "arabic", 1, 1, title, 1.0)
                   for index, title in enumerate(titles, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 1, 1, 1, 1, 1, 1])

    def test_same_page_visual_difference_is_not_treated_as_cross_page_drift(self) -> None:
        rows = [("Chapter 5. The Eye", 1), ("5.1 Introduction", 2),
                ("5.2 Special Topic", 1), ("5.3 Closing", 2)]
        entries = [TocEntry(title, index, "arabic", level, 7, title, 1.0)
                   for index, (title, level) in enumerate(rows, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 2, 1, 2])

    def test_repaired_levels_are_written_as_nested_pdf_bookmarks(self) -> None:
        titles_and_model_levels = [
            ("第1章 基础", 1), ("1.10 Kronecker 积", 2),
            ("1.10.1 定义", 3), ("本章小结", 2),
            ("第2章 特殊矩阵", 1), ("2.1 Hermitian 矩阵", 2),
            ("2.8 Fourier 矩阵", 2), ("2.8.1 定义", 3),
            ("2.8.2 计算", 3),
        ]
        entries = normalize_levels([
            TocEntry(title, 1, "arabic", level, 1, title, 1.0, pdf_page=1)
            for title, level in titles_and_model_levels
        ])
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.pdf"
            output = Path(temporary) / "output.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=300)
            writer.write(source)
            _write_pdf(source, output, entries)

            actual: list[tuple[str, int]] = []

            def walk(items: list, level: int = 1) -> None:
                for item in items:
                    if isinstance(item, list):
                        walk(item, level + 1)
                    else:
                        actual.append((str(item["/Title"]), level))

            walk(PdfReader(output).outline)
        self.assertEqual(actual, [(title, level) for (title, _), level in zip(
            titles_and_model_levels, [1, 2, 3, 2, 1, 2, 2, 3, 3])])

    def test_unnumbered_chapter_items_are_siblings_of_numbered_sections_in_pdf(self) -> None:
        # Two printed TOC pages: the model can occasionally call an indented
        # unnumbered item level 1, although it repeats at the end of chapters.
        titles_and_visual_levels = [
            ("Chapter 4. Prisms and Mirrors", 1, 7),
            ("4.17 Analysis of Fabrication Errors", 2, 7),
            ("Bibliography", 1, 7),
            ("Chapter 5. The Eye", 1, 8),
            ("5.1 Introduction", 2, 8),
            ("5.4 Defects of the Eye", 2, 8),
            ("Bibliography", 1, 8),
            ("Exercises", 1, 8),
            ("Chapter 6. Stops and Apertures", 1, 9),
            ("6.1 Introduction", 2, 9),
            ("Further Reading", 2, 9),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, source_page, title, 1.0,
                     pdf_page=1)
            for index, (title, level, source_page) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries],
                         [1, 2, 2, 1, 2, 2, 2, 2, 1, 2, 2])

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.pdf"
            output = Path(temporary) / "output.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=300)
            writer.write(source)
            _write_pdf(source, output, entries)

            parented: list[tuple[str, str | None]] = []

            def walk(items: list, parent: str | None = None) -> None:
                current: str | None = None
                for item in items:
                    if isinstance(item, list):
                        walk(item, current)
                    else:
                        current = str(item["/Title"])
                        parented.append((current, parent))

            walk(PdfReader(output).outline)

        self.assertEqual(parented, [
            ("Chapter 4. Prisms and Mirrors", None),
            ("4.17 Analysis of Fabrication Errors", "Chapter 4. Prisms and Mirrors"),
            ("Bibliography", "Chapter 4. Prisms and Mirrors"),
            ("Chapter 5. The Eye", None),
            ("5.1 Introduction", "Chapter 5. The Eye"),
            ("5.4 Defects of the Eye", "Chapter 5. The Eye"),
            ("Bibliography", "Chapter 5. The Eye"),
            ("Exercises", "Chapter 5. The Eye"),
            ("Chapter 6. Stops and Apertures", None),
            ("6.1 Introduction", "Chapter 6. Stops and Apertures"),
            ("Further Reading", "Chapter 6. Stops and Apertures"),
        ])

    def test_standalone_book_level_bibliography_remains_top_level(self) -> None:
        titles_and_levels = [
            ("Preface", 1),
            ("Chapter 5. The Eye", 1),
            ("5.1 Introduction", 2),
            ("Bibliography", 1),
            ("Index", 1),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries], [1, 1, 2, 1, 1])

    def test_single_unnumbered_endings_follow_sections_before_next_chapter(self) -> None:
        titles_and_visual_levels = [
            ("Chapter 5. The Eye", 1),
            ("5.1 Introduction", 2),
            ("Bibliography", 1),
            ("Further Reading", 1),
            ("Notes", 1),
            ("Chapter 6. Stops and Apertures", 1),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries], [1, 2, 2, 2, 2, 1])

    def test_part_chapters_and_unnumbered_rows_follow_visual_levels(self) -> None:
        titles_and_visual_levels = [
            ("Part I. Foundations", 1),
            ("Chapter 1. Basic Concepts", 2),
            ("1.1 Introduction", 3),
            ("Further Reading", 3),
            ("Chapter 2. Applications", 2),
            ("2.1 Methods", 3),
            ("Notes", 3),
            ("Part II. Practice", 1),
            ("Chapter 3. Design", 2),
            ("3.1 Examples", 3),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries],
                         [1, 2, 3, 3, 2, 3, 3, 1, 2, 3])

    def test_numbered_and_unnumbered_rows_at_same_visual_indent_remain_peers(self) -> None:
        titles_and_visual_levels = [
            ("Chapter 5. The Eye", 1),
            ("5.1 Introduction", 2),
            ("5.1.1 Additional Detail", 2),
            ("Practical Notes", 2),
            ("5.1.2 Summary", 2),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries], [1, 2, 2, 2, 2])

    def test_book_level_appendix_is_not_absorbed_into_last_chapter(self) -> None:
        titles_and_visual_levels = [
            ("Chapter 5. The Eye", 1),
            ("5.1 Introduction", 2),
            ("Bibliography", 1),
            ("Appendix A. Tables", 1),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries], [1, 2, 1, 1])

    def test_printed_page_label_accepts_parenthesized_arabic_and_roman(self) -> None:
        self.assertEqual(_page_value("（１）"), ("arabic", 1))
        self.assertEqual(_page_value("iv"), ("roman", 4))
        self.assertIsNone(_page_value("0"))


if __name__ == "__main__":
    unittest.main()
