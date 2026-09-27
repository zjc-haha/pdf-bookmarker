from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from bookmarker.pipeline import _write_pdf
from bookmarker.toc import TocEntry, _page_value, normalize_levels, toc_page_bookmark


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

    def test_chapter_appendices_and_introduction_sections_follow_toc_indent(self) -> None:
        # The layout of 《光学教程》: indented "附录 1.1" rows belong to their
        # chapter, and "0.1" sections belong to the unnumbered "绪论".
        titles_and_visual_levels = [
            ("绪论", 1),
            ("0.1 光学的研究内容和方法", 2),
            ("0.2 光学发展简史", 2),
            ("第1章 光的干涉", 1),
            ("1.1 波动的独立性、叠加性和相干性", 2),
            ("1.10 光的干涉应用举例 牛顿环", 2),
            ("视窗与链接 增透膜与高反射膜", 2),
            ("附录 1.1 振动叠加的三种计算方法", 2),
            ("附录 1.2 简谐波的表达式 复振幅", 2),
            ("习题", 2),
            ("第2章 光的衍射", 1),
            ("2.1 惠更斯-菲涅耳原理", 2),
            ("附录 2.1 夫琅禾费单缝衍射公式的推导", 2),
            ("附录 A 常用物理常量", 1),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries],
                         [1, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 1])

    def test_sections_nest_under_unnumbered_chapter_headings(self) -> None:
        titles_and_visual_levels = [
            ("光的干涉", 1),
            ("1.1 相干性", 2),
            ("1.2 双缝干涉", 2),
            ("阅读材料", 2),
            ("光的衍射", 1),
            ("2.1 单缝衍射", 2),
        ]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0)
            for index, (title, level) in enumerate(titles_and_visual_levels, 1)
        ])
        self.assertEqual([entry.level for entry in entries], [1, 2, 2, 2, 1, 2])

    def test_indented_chapter_appendix_is_written_inside_its_chapter(self) -> None:
        rows = [("第1章 光的干涉", 1, 1), ("1.1 相干性", 2, 1),
                ("附录 1.1 振动叠加的三种计算方法", 2, 2), ("第2章 光的衍射", 1, 3)]
        entries = normalize_levels([
            TocEntry(title, index, "arabic", level, 1, title, 1.0, pdf_page=page)
            for index, (title, level, page) in enumerate(rows, 1)
        ])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            output = Path(directory) / "output.pdf"
            writer = PdfWriter()
            for _ in range(3):
                writer.add_blank_page(width=200, height=300)
            with source.open("wb") as stream:
                writer.write(stream)
            _write_pdf(source, output, entries)
            outline = PdfReader(output).outline
        self.assertEqual(str(outline[0]["/Title"]), "第1章 光的干涉")
        self.assertEqual([str(item["/Title"]) for item in outline[1]],
                         ["1.1 相干性", "附录 1.1 振动叠加的三种计算方法"])
        self.assertEqual(str(outline[2]["/Title"]), "第2章 光的衍射")

    def test_contents_page_bookmark_follows_the_language_of_the_toc(self) -> None:
        chinese = [TocEntry(title, page, "arabic", 1, 5, title, 1.0)
                   for page, title in enumerate(["绪论", "第1章 光的干涉", "Appendix A"], 1)]
        bookmark = toc_page_bookmark(chinese, 5)
        self.assertEqual((bookmark.title, bookmark.level, bookmark.pdf_page), ("目录", 1, 5))
        english = [TocEntry(title, page, "arabic", 1, 3, title, 1.0)
                   for page, title in enumerate(["Preface", "Chapter 1 Optics", "附录"], 1)]
        self.assertEqual(toc_page_bookmark(english, 3).title, "Contents")

    def test_printed_page_label_accepts_parenthesized_arabic_and_roman(self) -> None:
        self.assertEqual(_page_value("（１）"), ("arabic", 1))
        self.assertEqual(_page_value("iv"), ("roman", 4))
        self.assertIsNone(_page_value("0"))


if __name__ == "__main__":
    unittest.main()
