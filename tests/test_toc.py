from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader, PdfWriter

from bookmarker.extract import Extractor, Line, PageContent, Word, _usable_native_text
from bookmarker.pipeline import _write_pdf
from bookmarker.toc import TocEntry, normalize_levels, page_entries, parse_line


def line(words: list[tuple[str, float]], y: float) -> Line:
    items = tuple(Word(text, x, y, max(4.0, len(text) * 5.0), 11.0)
                  for text, x in words)
    return Line(" ".join(word.text for word in items), items,
                min(word.x for word in items), y,
                max(word.x + word.width for word in items), y + 11)


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

    def test_adjacent_digits_form_one_printed_page(self) -> None:
        row = line([("2.6", 61), ("Phasors", 85), ("and", 124),
                    ("Waves", 211), ("2", 250), ("3", 256)], 307)
        item = parse_line(row, 560, 6, "text")
        self.assertIsNotNone(item)
        self.assertEqual((item.title, item.printed_page),
                         ("2.6 Phasors and Waves", 23))

    def test_parenthesized_arabic_page_in_ocr_toc_row(self) -> None:
        for page_label in ("(1)", "（１）"):
            with self.subTest(page_label=page_label):
                row = line([("§0.1", 61), ("近世代数的创立", 110),
                            (page_label, 505)], 307)
                item = parse_line(row, 560, 11, "ocr")
                self.assertIsNotNone(item)
                self.assertEqual((item.title, item.printed_page,
                                  item.numbering),
                                 ("§0.1 近世代数的创立", 1, "arabic"))

    def test_wrapped_title_keeps_original_word_order(self) -> None:
        first = line([("4.9", 320), ("Familiar", 344),
                      ("Aspects", 391), ("of", 432),
                      ("the", 446), ("Interaction", 465),
                      ("of", 525)], 227)
        second = line([("Light", 320), ("and", 350),
                       ("Matter", 374), ("131", 515)], 239)
        content = PageContent(6, 560, 613, (first, second), "text")
        rows, _ = page_entries(content)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title,
                         "4.9 Familiar Aspects of the Interaction of Light and Matter")
        self.assertEqual(rows[0].printed_page, 131)

    def test_empty_ocr_page_does_not_render_number_montage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            extractor = Extractor(Path("missing.pdf"), Path(temporary))
            empty = PageContent(1, 600, 800, (), "ocr")
            with patch.object(extractor, "page", return_value=empty), \
                 patch.object(extractor, "_render") as render:
                self.assertIs(extractor.enhance_toc_numbers(None, 1), empty)
                render.assert_not_called()

    def test_broken_native_character_map_needs_ocr(self) -> None:
        good = [Word("A searchable paragraph about physics and experiments.", 0, 0, 100, 12)]
        bad = [Word("\ue000" * 50, 0, 0, 100, 12)]
        self.assertTrue(_usable_native_text(good))
        self.assertFalse(_usable_native_text(bad))

    def test_english_scan_uses_english_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            extractor = Extractor(Path("missing.pdf"), Path(temporary))
            chinese = {"lines": [{"text": "Wave M0tion"}]}
            english = {"lines": [{"text": "Wave Motion"}]}
            with patch.object(extractor, "_run_ocr_language", side_effect=[chinese, english]) as ocr:
                self.assertIs(extractor._run_ocr(Path("unused.png")), english)
                self.assertEqual(ocr.call_count, 2)

    def test_full_page_number_beats_distorted_margin_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            extractor = Extractor(Path("missing.pdf"), Path(temporary))
            title = Word("§ 11.2 贝塞尔方程", 40, 200, 140, 11)
            row = Line(title.text, (title,), 40, 200, 180, 211)
            page = PageContent(1, 500, 800, (row,), "ocr")
            full = {"lines": [{"words": [{"text": "241", "x": 900, "y": 250}]}]}
            margin = {"lines": [{"words": [{"text": "136", "x": 350, "y": 25}]}]}

            def render(_number: int, destination: Path, *, scale: int) -> None:
                Image.new("RGB", (1000, 1000), "white").save(destination)

            with patch.object(extractor, "page", return_value=page), \
                 patch.object(extractor, "_render", side_effect=render), \
                 patch.object(extractor, "_run_ocr_language", side_effect=[full, margin]):
                improved = extractor.enhance_toc_numbers(None, 1)
            self.assertIn("241", improved.lines[0].text)
            self.assertNotIn("136", improved.lines[0].text)

    def test_scanned_running_header_can_supply_page_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            extractor = Extractor(Path("missing.pdf"), Path(temporary))
            page = SimpleNamespace(width=500, height=800, extract_words=lambda **_: [])
            pdf = SimpleNamespace(pages=[page])
            raw = {"width": 1000, "lines": [{"text": "12 Chapter Three", "words": [
                {"text": "12", "x": 20}, {"text": "Chapter", "x": 95},
                {"text": "Three", "x": 220}]}]}

            def render(_number: int, destination: Path, *, scale: int) -> None:
                Image.new("RGB", (1000, 1000), "white").save(destination)

            with patch.object(extractor, "_render", side_effect=render), \
                 patch.object(extractor, "_run_ocr", return_value=raw):
                self.assertEqual(extractor.footer_labels(pdf, 1), [("arabic", 12)])


if __name__ == "__main__":
    unittest.main()
