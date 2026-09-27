from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from bookmarker.pipeline import _write_pdf
from bookmarker.toc import TocEntry, _page_value, normalize_levels


class TocParsingTest(unittest.TestCase):
    def test_numbered_titles_fix_inconsistent_model_levels_across_toc_pages(self) -> None:
        titles_and_levels = [
            ("第1章 矩阵代数基础", 1),
            ("1.9 矩阵的直和", 1),
            ("1.9.1 矩阵的直和", 2),
            ("本章小结", 1),
            ("习题", 1),
            ("第2章 特殊矩阵", 1),
            ("2.1 Hermitian 矩阵", 1),
            ("2.8 Fourier 矩阵", 1),
            ("2.8.1 Fourier 矩阵的定义", 2),
            ("2.8.2 适定方程", 3),
            ("2.8.3 FFT 算法", 3),
            ("2.9 Hadamard 矩阵", 2),
            ("本章小结", 2),
            ("第3章 矩阵微分", 1),
        ]
        entries = [TocEntry(title, index, "arabic", level, 10 if index < 10 else 11,
                            title, 1.0)
                   for index, (title, level) in enumerate(titles_and_levels, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 2, 3, 2, 2, 1, 2, 2, 3, 3, 3, 2, 2, 1])

    def test_numbered_children_without_parent_do_not_nest_under_siblings(self) -> None:
        titles = ["第2章 特殊矩阵", "2.8.1 定义", "2.8.2 性质", "2.9 其它"]
        entries = [TocEntry(title, index, "arabic", 3, 1, title, 1.0)
                   for index, title in enumerate(titles, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 2, 2, 2])

    def test_chinese_chapter_title_supplies_parent_for_arabic_sections(self) -> None:
        titles = ["第一章 基础", "1.1 矩阵", "1.1.1 向量", "1.2 运算", "第二章 应用",
                  "2.8.1 定义", "2.8.2 性质"]
        entries = [TocEntry(title, index, "arabic", 1, 1, title, 1.0)
                   for index, title in enumerate(titles, 1)]
        self.assertEqual([entry.level for entry in normalize_levels(entries)],
                         [1, 2, 3, 2, 1, 2, 2])

    def test_repaired_levels_are_written_as_nested_pdf_bookmarks(self) -> None:
        titles_and_model_levels = [
            ("第1章 基础", 1), ("1.10 Kronecker 积", 1),
            ("1.10.1 定义", 2), ("本章小结", 1),
            ("第2章 特殊矩阵", 1), ("2.1 Hermitian 矩阵", 1),
            ("2.8 Fourier 矩阵", 1), ("2.8.1 定义", 2),
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

    def test_printed_page_label_accepts_parenthesized_arabic_and_roman(self) -> None:
        self.assertEqual(_page_value("（１）"), ("arabic", 1))
        self.assertEqual(_page_value("iv"), ("roman", 4))
        self.assertIsNone(_page_value("0"))


if __name__ == "__main__":
    unittest.main()
