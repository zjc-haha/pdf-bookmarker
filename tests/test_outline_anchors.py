"""Page-number outline anchors must be verified before they map a scanned book."""

from __future__ import annotations

from io import BytesIO
import unittest

from pypdf import PdfReader, PdfWriter

from bookmarker.pipeline import (_outline_page_anchors,
                                 _verified_outline_page_anchors)


def make_reader(numbers: list[tuple[str, int]], *, pages: int = 191,
                frontmatter: bool = False) -> PdfReader:
    """Make an in-memory scanned-style PDF with title/destination pairs."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=300)
    if frontmatter:
        for title, page in [("封面", 1), ("书名", 3), ("版权", 4),
                            ("前言", 5), ("目录", 10)]:
            writer.add_outline_item(title, page - 1)
    for title, page in numbers:
        writer.add_outline_item(title, page - 1)
    stream = BytesIO()
    writer.write(stream)
    stream.seek(0)
    return PdfReader(stream)


def page_numbers(*, offset: int = 10, count: int = 181) -> list[tuple[str, int]]:
    return [(str(number), number + offset) for number in range(1, count + 1)]


class FakePageLabels:
    def __init__(self, offset: int, *, conflict_middle: bool = False) -> None:
        self.offset = offset
        self.conflict_middle = conflict_middle
        self.prefetched: list[int] = []

    def prefetch(self, _pdf: object, numbers: list[int]) -> None:
        self.prefetched.extend(numbers)

    def footer_labels(self, _pdf: object, page_number: int) -> list[tuple[str, int]]:
        offset = self.offset + (1 if self.conflict_middle and 80 <= page_number <= 120
                                else 0)
        return [("arabic", page_number - offset)]


class OutlineAnchorTest(unittest.TestCase):
    def test_real_scanned_book_pattern_gives_five_spread_anchors(self) -> None:
        reader = make_reader(page_numbers(), frontmatter=True)
        anchors = _outline_page_anchors(reader, [10])
        self.assertEqual(len(anchors), 5)
        self.assertEqual({anchor.numbering for anchor in anchors}, {"arabic"})
        self.assertEqual({anchor.offset for anchor in anchors}, {10})
        self.assertLessEqual(anchors[0].pdf_page, 25)
        self.assertGreaterEqual(anchors[-1].pdf_page, 170)
        self.assertGreaterEqual(anchors[-1].pdf_page - anchors[0].pdf_page, 150)

    def test_repeated_and_out_of_order_labels_are_not_page_anchors(self) -> None:
        repeated = page_numbers()
        repeated[79] = ("79", 90)
        self.assertEqual(_outline_page_anchors(make_reader(repeated), [5]), [])

        reordered = page_numbers()
        reordered[49], reordered[50] = reordered[50], reordered[49]
        self.assertEqual(_outline_page_anchors(make_reader(reordered), [5]), [])

    def test_offset_change_and_short_coverage_are_rejected(self) -> None:
        changed = [(str(number), number + (9 if number <= 90 else 10))
                   for number in range(1, 182)]
        self.assertEqual(_outline_page_anchors(make_reader(changed), [5]), [])
        self.assertEqual(_outline_page_anchors(make_reader(page_numbers(count=12)),
                                               [5]), [])

    def test_toc_destination_conflict_is_rejected(self) -> None:
        # Numeric title 50 points at PDF page 60, which is a known TOC page.
        self.assertEqual(_outline_page_anchors(make_reader(page_numbers()), [60]), [])

    def test_leading_zero_and_section_number_titles_are_not_page_labels(self) -> None:
        leading_zero = [(f"0{number}", number + 10) for number in range(1, 182)]
        sections = [(f"{number}.1", number + 10) for number in range(1, 182)]
        self.assertEqual(_outline_page_anchors(make_reader(leading_zero), [5]), [])
        self.assertEqual(_outline_page_anchors(make_reader(sections), [5]), [])

    def test_image_page_labels_confirm_or_veto_outline_offset(self) -> None:
        reader = make_reader(page_numbers(), frontmatter=True)
        matching = FakePageLabels(10)
        anchors = _verified_outline_page_anchors(reader, reader, matching, [10])
        self.assertEqual(len(anchors), 5)
        self.assertEqual({anchor.offset for anchor in anchors}, {10})
        self.assertGreaterEqual(len(matching.prefetched), 3)

        forged = FakePageLabels(9)
        self.assertEqual(_verified_outline_page_anchors(reader, reader, forged, [10]), [])

        conflicting = FakePageLabels(10, conflict_middle=True)
        self.assertEqual(_verified_outline_page_anchors(reader, reader, conflicting,
                                                        [10]), [])


if __name__ == "__main__":
    unittest.main()
