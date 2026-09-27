"""Checks for page-offset and outline helpers shared with DeepSeek."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from reportlab.pdfgen import canvas

from bookmarker.pipeline import (Anchor, _fit_offsets, _outline_action,
                                 _outline_page_label_ratio, _outline_quality,
                                 _title_anchors)
from bookmarker.toc import TocEntry


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
