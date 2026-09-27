from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from bookmarker.pipeline import (Anchor, _fit_offsets, _outline_action,
                                 _outline_page_label_ratio, _outline_quality,
                                 _title_anchors, process_book)
from bookmarker.toc import TocEntry


def make_searchable_book(path: Path, *, with_existing: bool = False,
                         roman_frontmatter: bool = False) -> None:
    book = canvas.Canvas(str(path), pagesize=(612, 792))
    book.setFont("Helvetica-Bold", 22)
    book.drawString(72, 700, "Sample Physics Book")
    book.showPage()

    book.setFont("Helvetica-Bold", 18)
    book.drawString(72, 720, "Contents")
    rows = [
        ("Chapter 1 Introduction", 1),
        ("1.1 Background", 2),
        ("Chapter 2 Methods", 4),
        ("2.1 Setup", 6),
        ("Appendix", 7),
    ]
    if roman_frontmatter:
        rows = [("Foreword", "i"), ("Preface", "iii")] + rows
    book.setFont("Helvetica", 13)
    for index, (title, printed) in enumerate(rows):
        y = 650 - index * 37
        book.drawString(72, y, f"{title} ................")
        book.drawRightString(540, y, str(printed))
    book.showPage()

    book.drawString(72, 650, "Preface")
    if with_existing:
        book.bookmarkPage("original-preface")
        book.addOutlineEntry("Original Preface", "original-preface")
    book.showPage()
    body = {1: "Chapter 1 Introduction", 2: "1.1 Background", 4: "Chapter 2 Methods",
            6: "2.1 Setup", 7: "Appendix"}
    for printed in range(1, 8):
        book.setFont("Helvetica-Bold", 18)
        book.drawString(72, 700, body.get(printed, "Physics body text"))
        book.setFont("Helvetica", 12)
        book.drawString(72, 600, "A searchable paragraph about physics and experiments.")
        book.drawCentredString(306, 34, str(printed))
        book.showPage()
    book.save()


def make_book_with_inserted_page(path: Path) -> None:
    book = canvas.Canvas(str(path), pagesize=(612, 792))
    book.setFont("Helvetica-Bold", 20)
    book.drawString(72, 700, "Physics with an inserted page")
    book.showPage()
    book.drawString(72, 720, "Contents")
    rows = [("Chapter 1 Start", 1), ("Chapter 2 Matter", 8),
            ("Chapter 3 Energy", 14), ("Chapter 4 Waves", 20),
            ("Appendix", 24)]
    book.setFont("Helvetica", 13)
    for index, (title, printed) in enumerate(rows):
        book.drawString(72, 650 - index * 36, f"{title} ................")
        book.drawRightString(540, 650 - index * 36, str(printed))
    book.showPage()
    for printed in range(1, 25):
        if printed == 14:
            book.drawString(72, 700, "Inserted unnumbered plate")
            book.showPage()
        book.drawString(72, 700, f"Body content on printed page {printed}")
        book.drawCentredString(306, 34, str(printed))
        book.showPage()
    book.save()


def add_existing_bookmarks(path: Path, titles: list[str]) -> None:
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.append(reader, import_outline=False)
    for index, title in enumerate(titles):
        writer.add_outline_item(title, index % len(reader.pages))
    temporary = path.with_name(path.stem + "-with-outline.pdf")
    with temporary.open("wb") as stream:
        writer.write(stream)
    temporary.replace(path)


EXPECTED_TOC_OUTLINE = [
    ("Chapter 1 Introduction", 4, 1),
    ("1.1 Background", 5, 2),
    ("Chapter 2 Methods", 7, 1),
    ("2.1 Setup", 9, 2),
    ("Appendix", 10, 1),
]


def add_structured_bookmarks(path: Path, entries: list[tuple[str, int, int]]) -> None:
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.append(reader, import_outline=False)
    parents = {}
    for title, page, level in entries:
        parent = parents.get(level - 1) if level > 1 else None
        parents[level] = writer.add_outline_item(title, page - 1, parent=parent)
    temporary = path.with_name(path.stem + "-with-structured-outline.pdf")
    with temporary.open("wb") as stream:
        writer.write(stream)
    temporary.replace(path)


def flat_outline(reader: PdfReader, items: list) -> list[tuple[str, int]]:
    found = []
    for item in items:
        if isinstance(item, list):
            found.extend(flat_outline(reader, item))
        else:
            found.append((item["/Title"], reader.get_destination_page_number(item) + 1))
    return found


class PipelineTest(unittest.TestCase):
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

    def test_conflicting_ocr_labels_on_same_pages_do_not_create_segments(self) -> None:
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

    def test_searchable_pdf_gets_bookmarks_with_calibrated_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "bookmarked.pdf"
            make_searchable_book(source)
            result = process_book(source, output, root / "cache", ocr="off")
            self.assertEqual(result.status, "success", result.as_dict())
            self.assertEqual(result.toc_pages, [2])
            self.assertEqual(result.offsets["arabic"], 3)
            self.assertTrue(output.exists())
            pdf = PdfReader(output)
            self.assertEqual(len(pdf.pages), 10)
            self.assertEqual(pdf.metadata.title, PdfReader(source).metadata.title)
            self.assertEqual([item["/Title"] for item in pdf.outline if not isinstance(item, list)],
                             ["Chapter 1 Introduction", "Chapter 2 Methods", "Appendix"])
            self.assertEqual([item["/Title"] for item in pdf.outline[1]], ["1.1 Background"])
            self.assertEqual([item["/Title"] for item in pdf.outline[3]], ["2.1 Setup"])
            self.assertEqual(pdf.get_destination_page_number(pdf.outline[2]), 6)

    def test_ocr_pipeline_omits_roman_entries_and_keeps_numbered_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "bookmarked.pdf"
            make_searchable_book(source, roman_frontmatter=True)
            result = process_book(source, output, root / "cache", ocr="off")

            self.assertEqual(result.status, "success", result.as_dict())
            self.assertEqual(result.offsets, {"arabic": 3})
            self.assertEqual([entry["title"] for entry in result.entries],
                             [title for title, _, _ in EXPECTED_TOC_OUTLINE])
            pdf = PdfReader(output)
            self.assertEqual(flat_outline(pdf, pdf.outline),
                             [(title, page) for title, page, _ in EXPECTED_TOC_OUTLINE])

    def test_page_offset_change_after_inserted_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "segmented.pdf"
            output = root / "bookmarked.pdf"
            make_book_with_inserted_page(source)
            result = process_book(source, output, root / "cache", ocr="off")
            self.assertEqual(result.status, "success", result.as_dict())
            self.assertEqual(result.offset_segments[0]["offset"], 2)
            self.assertEqual(result.offset_segments[1]["offset"], 3)
            pdf = PdfReader(output)
            self.assertEqual(len(pdf.pages), 27)
            self.assertEqual([pdf.get_destination_page_number(item) + 1 for item in pdf.outline],
                             [3, 10, 17, 23, 27])

    def test_sparse_existing_bookmarks_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "bookmarked.pdf"
            make_searchable_book(source, with_existing=True)
            result = process_book(source, output, root / "cache", ocr="off")
            self.assertEqual(result.status, "success", result.as_dict())
            pdf = PdfReader(output)
            self.assertEqual(pdf.outline[0]["/Title"], "Original Preface")
            self.assertEqual(pdf.get_destination_page_number(pdf.outline[0]) + 1, 3)
            self.assertEqual(pdf.outline[1]["/Title"], "自动识别目录")
            self.assertEqual(len(pdf.outline[2]), 5)

    def test_skip_bookmarked_short_circuits_before_ocr_even_when_replacement_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            make_searchable_book(source, with_existing=True)
            original = source.read_bytes()
            with patch("bookmarker.pipeline.Extractor", side_effect=AssertionError("OCR started")):
                result = process_book(source, source, root / "cache", ocr="off",
                                      skip_existing=False, verify_existing=True,
                                      skip_bookmarked=True)
            self.assertEqual(result.status, "skipped", result.as_dict())
            self.assertEqual(result.existing_bookmarks, 1)
            self.assertEqual(result.toc_pages, [])
            self.assertEqual(source.read_bytes(), original)

    def test_skip_bookmarked_does_not_skip_pdf_without_bookmarks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "bookmarked.pdf"
            make_searchable_book(source)
            result = process_book(source, output, root / "cache", ocr="off",
                                  skip_bookmarked=True)
            self.assertEqual(result.status, "success", result.as_dict())
            self.assertTrue(output.is_file())

    def test_dense_page_number_bookmarks_are_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "bookmarked.pdf"
            make_searchable_book(source)
            add_existing_bookmarks(source, ["封面", "书名", "版权", "前言", "目录"]
                                   + [str(index) for index in range(1, 182)])
            result = process_book(source, output, root / "cache", ocr="off")
            self.assertEqual(result.status, "success", result.as_dict())
            self.assertEqual(result.existing_bookmarks, 186)
            pdf = PdfReader(output)
            self.assertEqual(len(pdf.outline), 5)
            self.assertNotIn("封面", [item["/Title"] for item in pdf.outline
                                     if not isinstance(item, list)])

    def test_sparse_short_chinese_bookmark_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "bookmarked.pdf"
            make_searchable_book(source)
            add_existing_bookmarks(source, ["前言"])
            result = process_book(source, output, root / "cache", ocr="off")
            self.assertEqual(result.status, "success", result.as_dict())
            pdf = PdfReader(output)
            self.assertEqual(pdf.outline[0]["/Title"], "前言")
            self.assertEqual(pdf.outline[1]["/Title"], "自动识别目录")

    def test_dense_useful_chapter_bookmarks_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "bookmarked.pdf"
            make_searchable_book(source)
            add_existing_bookmarks(source, [f"第 {index} 章 正文{index}" for index in range(1, 41)])
            result = process_book(source, output, root / "cache", ocr="off")
            self.assertEqual(result.status, "skipped", result.as_dict())
            self.assertFalse(output.exists())

    def test_verify_existing_skips_outline_matching_printed_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "bookmarked.pdf"
            make_searchable_book(source)
            add_structured_bookmarks(source, EXPECTED_TOC_OUTLINE)

            result = process_book(source, output, root / "cache", ocr="off",
                                  verify_existing=True)

            self.assertEqual(result.status, "skipped", result.as_dict())
            self.assertEqual(result.toc_pages, [2])
            self.assertTrue(result.existing_outline_check["matches"])
            self.assertFalse(output.exists())

    def test_verify_existing_replaces_missing_or_incorrect_outline(self) -> None:
        cases = {
            "missing section": EXPECTED_TOC_OUTLINE[:3] + EXPECTED_TOC_OUTLINE[4:],
            "incorrect title": EXPECTED_TOC_OUTLINE[:2]
                               + [("Chapter 2 Outdated", 7, 1)]
                               + EXPECTED_TOC_OUTLINE[3:],
            "incorrect chapter number": EXPECTED_TOC_OUTLINE[:2]
                                        + [("Chapter 3 Methods", 7, 1)]
                                        + EXPECTED_TOC_OUTLINE[3:],
            "incorrect section number": EXPECTED_TOC_OUTLINE[:1]
                                        + [("11 Background", 5, 2)]
                                        + EXPECTED_TOC_OUTLINE[2:],
            "incorrect page": EXPECTED_TOC_OUTLINE[:2]
                              + [("Chapter 2 Methods", 8, 1)]
                              + EXPECTED_TOC_OUTLINE[3:],
        }
        for name, outline in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.pdf"
                output = root / "bookmarked.pdf"
                make_searchable_book(source)
                add_structured_bookmarks(source, outline)

                result = process_book(source, output, root / "cache", ocr="off",
                                      verify_existing=True)

                self.assertEqual(result.status, "success", result.as_dict())
                self.assertFalse(result.existing_outline_check["matches"])
                pdf = PdfReader(output)
                self.assertEqual(flat_outline(pdf, pdf.outline),
                                 [(title, page) for title, page, _ in EXPECTED_TOC_OUTLINE])
                self.assertEqual(len(pdf.outline), 5)

    def test_verify_existing_does_not_write_when_contents_are_untrusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "bookmarked.pdf"
            make_searchable_book(source)
            add_structured_bookmarks(source, EXPECTED_TOC_OUTLINE)
            original = source.read_bytes()

            with patch("bookmarker.pipeline._find_toc",
                       return_value=([], [], ["未找到可信目录"], [])):
                result = process_book(source, output, root / "cache", ocr="off",
                                      verify_existing=True)

            self.assertEqual(result.status, "needs_review", result.as_dict())
            self.assertFalse(output.exists())
            self.assertEqual(source.read_bytes(), original)

    def test_in_place_replace_holds_unrelated_useful_outline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            make_searchable_book(source)
            unrelated = [(f"Chapter {index} Unrelated", page, 1)
                         for index, page in enumerate((4, 5, 7, 9, 10), 1)]
            add_structured_bookmarks(source, unrelated)
            original = source.read_bytes()

            result = process_book(source, source, root / "cache", ocr="off",
                                  verify_existing=True)

            self.assertEqual(result.status, "needs_review", result.as_dict())
            self.assertEqual(result.existing_outline_check["matched_titles"], 0)
            self.assertTrue(any("避免直接覆盖" in warning for warning in result.warnings))
            self.assertEqual(source.read_bytes(), original)

            forced = process_book(source, source, root / "cache", ocr="off",
                                  verify_existing=True, skip_existing=False)
            self.assertEqual(forced.status, "success", forced.as_dict())
            pdf = PdfReader(source)
            self.assertEqual(flat_outline(pdf, pdf.outline),
                             [(title, page) for title, page, _ in EXPECTED_TOC_OUTLINE])


if __name__ == "__main__":
    unittest.main()
