"""Shared table-of-contents entries and bookmark hierarchy normalization."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Iterable

from .extract import _roman_to_int, clean_text


# Changing outline construction must invalidate successful --resume records.
# TOC prompt/cache revisions are tracked separately in deepseek.py.
HIERARCHY_VERSION = 9


@dataclass
class TocEntry:
    title: str
    printed_page: int
    numbering: str
    level: int
    source_page: int
    raw: str
    confidence: float
    pdf_page: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


PART = re.compile(
    r"^(?:第\s*[一二三四五六七八九十百零〇0-9]+\s*[篇部卷]|parts?\s+(?:[ivxlcdm]+|\d+)\b)",
    re.I,
)
CHAPTER = re.compile(r"^(?:第\s*[一二三四五六七八九十百零〇0-9]+\s*章|chapters?\s+\d+|附录|appendix)", re.I)
NUMBERED = re.compile(
    r"^(\d{1,3}(?:\s*[.．]\s*\d{1,3}){0,5})(?:\s*[.．、](?!\d))?(?![\d.．])\s*",
    re.I,
)
APPENDIX = re.compile(r"^(?:附录|appendix\b)", re.I)
# A heading numbered with an upper-case Roman numeral, such as "I The Calculus
# of variations".  L, C, D and M are left out so "C Programming" is not one.
ROMAN_HEADING = re.compile(r"^[IVX]{1,6}\s*[.．、:]?\s+(?=[^\W\d_])")
CHAPTER_NUMBER = re.compile(r"^(?:第\s*(\d{1,3})\s*章|chapters?\s+(\d{1,3}))", re.I)
INTRINSIC_CHAPTER_END = re.compile(r"^(?:本章|章末)")
BOOK_BOUNDARY = re.compile(
    r"^(?:附录|索引|后记|前言|序言|致谢|appendix\b|index\b|afterword\b|"
    r"preface\b|acknowledg\w*\b)", re.I,
)


def _arabic_page_digits(text: str) -> str | None:
    """Return digits from a plain or parenthesized printed page label."""
    compact = re.sub(r"\s+", "", clean_text(text))
    match = re.fullmatch(r"(?:([0-9]+)|\(([0-9]+)\))", compact)
    return (match.group(1) or match.group(2)) if match else None


def _page_value(text: str) -> tuple[str, int] | None:
    text = re.sub(r"\s+", "", clean_text(text))
    digits = _arabic_page_digits(text)
    if digits is not None and len(digits) <= 4:
        value = int(digits)
        return ("arabic", value) if value > 0 else None
    if re.fullmatch(r"[ivxlcdm]{1,8}", text, re.I):
        return ("roman", _roman_to_int(text))
    return None


def _numbered_parts(title: str) -> tuple[int, ...] | None:
    match = NUMBERED.match(title)
    if not match:
        return None
    return tuple(int(part) for part in re.split(r"\s*[.．]\s*", match.group(1)))


def _repeated_unnumbered_titles(entries: list[TocEntry]) -> Counter[str]:
    """Find recurring chapter furniture without relying on English keywords."""
    return Counter(
        clean_text(entry.title).casefold()
        for entry in entries
        if not PART.match(entry.title) and not CHAPTER.match(entry.title)
        and not _numbered_parts(entry.title)
    )


def _followed_by_chapter(entries: list[TocEntry], index: int) -> bool:
    """Detect a run of chapter-end headings before the next regular chapter."""
    for following in entries[index + 1:]:
        if PART.match(following.title) or BOOK_BOUNDARY.match(following.title):
            return False
        if CHAPTER.match(following.title):
            return True
        parts = _numbered_parts(following.title)
        if parts:
            return len(parts) == 1
    return False


def _visual_numbered_levels(entries: list[TocEntry]) -> list[int]:
    """Correct an isolated outlier only when neighboring peers agree."""
    levels = [entry.level for entry in entries]
    groups: dict[tuple[int, int], list[int]] = {}
    chapter_group = 0
    for index, entry in enumerate(entries):
        parts = _numbered_parts(entry.title)
        if PART.match(entry.title) or CHAPTER.match(entry.title) or (parts and len(parts) == 1):
            chapter_group += 1
        if parts and len(parts) > 1 and entry.level:
            groups.setdefault((chapter_group, len(parts)), []).append(index)
    reported = levels.copy()
    for indices in groups.values():
        for position in range(1, len(indices) - 1):
            before, current, after = (indices[position - 1], indices[position],
                                      indices[position + 1])
            crosses_page = (entries[current].source_page != entries[before].source_page
                            or entries[current].source_page != entries[after].source_page)
            if crosses_page and reported[before] == reported[after] != reported[current]:
                levels[current] = reported[before]
    return levels


def _anchors_page_levels(title: str) -> bool:
    """A part or chapter row gives its page an absolute level reference."""
    parts = _numbered_parts(title)
    return bool(PART.match(title) or (CHAPTER.match(title) and not APPENDIX.match(title))
                or (parts and len(parts) == 1))


def _realign_continuation_pages(entries: list[TocEntry]) -> None:
    """Undo a page-wide level shift at the top of a continuation TOC page.

    A contents page that begins inside a chapter shows no chapter row, so the
    model may count levels from that page's leftmost indent: in 《光学原理》 the
    page starting with 4.1.5 and 4.2 came back one level too shallow.  A
    numbering depth keeps one level throughout a TOC, so numbered rows reveal
    the shift.  Rows before the page's first part or chapter row move together
    when most of their numbered rows agree on the same shift.
    """
    depth_levels: dict[int, Counter[int]] = {}
    pages: list[list[TocEntry]] = []
    for entry in entries:
        if pages and pages[-1][0].source_page == entry.source_page:
            pages[-1].append(entry)
        else:
            pages.append([entry])
    for index, page in enumerate(pages):
        if index:
            leading: list[TocEntry] = []
            for entry in page:
                if _anchors_page_levels(entry.title):
                    break
                leading.append(entry)
            shifts = []
            for entry in leading:
                parts = _numbered_parts(entry.title)
                known = depth_levels.get(len(parts)) if parts and len(parts) > 1 else None
                if known and entry.level:
                    shifts.append(known.most_common(1)[0][0] - entry.level)
            if len(shifts) >= 2:
                shift, votes = Counter(shifts).most_common(1)[0]
                if shift and votes * 3 >= len(shifts) * 2:
                    for entry in leading:
                        if entry.level:
                            entry.level = max(1, min(entry.level + shift, 6))
        for entry in page:
            parts = _numbered_parts(entry.title)
            if parts and len(parts) > 1 and entry.level:
                depth_levels.setdefault(len(parts), Counter())[entry.level] += 1


def normalize_levels(entries: Iterable[TocEntry]) -> list[TocEntry]:
    result = list(entries)
    if not result:
        return result
    _realign_continuation_pages(result)
    explicit = [item.level for item in result if item.level]
    fallback = min(explicit) if explicit else 1
    visual_levels = _visual_numbered_levels(result)
    repeated_titles = _repeated_unnumbered_titles(result)
    previous = 0
    active_numbered: dict[tuple[int, ...], int] = {}
    part_open = False
    part_level = 0
    chapter_open = False
    chapter_level = 0
    chapter_major: int | None = None
    # Level of an open unnumbered heading such as "绪论", which owns the
    # indented "0.1" sections that follow it.
    heading_level: int | None = None
    # Level of the latest Roman-numbered heading, which owns the arabic
    # "1", "2" items after it (Born & Wolf's appendices I, II, III).
    roman_level: int | None = None
    for index, entry in enumerate(result):
        title = entry.title
        parts = _numbered_parts(title)
        opens_heading = False
        unnumbered = False
        roman_item = False
        if PART.match(title):
            level = entry.level or 1
            part_open = True
            part_level = max(1, min(level, previous + 1, 6))
            chapter_open = False
            chapter_level = 0
            chapter_major = None
            heading_level = None
            roman_level = None
            active_numbered.clear()
        elif APPENDIX.match(title) and chapter_open and entry.level > chapter_level:
            # "附录 1.1" indented like the chapter's sections belongs to that
            # chapter. Only an appendix at chapter level is a book division.
            level = entry.level
        elif CHAPTER.match(title):
            # A chapter is usually at the root, but can be visibly indented
            # below a Part. Numbering alone does not decide its level.
            level = (entry.level if part_open and entry.level > part_level else 1)
            chapter_open = True
            heading_level = None
            roman_level = None
            active_numbered.clear()
            chapter_match = CHAPTER_NUMBER.match(title)
            chapter_major = (int(chapter_match.group(1) or chapter_match.group(2))
                             if chapter_match else None)
            chapter_level = max(1, min(level, previous + 1, 6))
            if chapter_major is not None:
                active_numbered[(chapter_major,)] = chapter_level
        elif parts:
            if len(parts) == 1 and roman_level is not None:
                # "1 Euler's equations" under "I The Calculus of variations"
                # is an item of that heading, not an arabic-numbered chapter.
                level = roman_level + 1
                roman_item = True
            elif len(parts) == 1:
                active_numbered.clear()
                chapter_open = True
                heading_level = None
                chapter_major = parts[0]
                level = (visual_levels[index] if part_open and visual_levels[index] > part_level else 1)
                chapter_level = max(1, min(level, previous + 1, 6))
            else:
                if chapter_major is not None and parts[0] != chapter_major:
                    active_numbered.clear()
                    chapter_open = False
                    chapter_level = 0
                if chapter_major != parts[0]:
                    chapter_major = parts[0]
                    if chapter_open:
                        active_numbered[(parts[0],)] = chapter_level
                active_numbered = {
                    prefix: parent_level for prefix, parent_level in active_numbered.items()
                    if parts[:len(prefix)] == prefix
                }
                parent = active_numbered.get(parts[:-1])
                if parent is not None:
                    deepest_plausible = parent + 1
                elif chapter_open and (parts[0],) in active_numbered:
                    deepest_plausible = chapter_level + 1
                elif heading_level is not None:
                    deepest_plausible = heading_level + 1
                else:
                    deepest_plausible = 1
                # A printed parent and child can share one visual level. Use
                # numbering to cap impossible jumps, not to force indentation.
                level = (min(visual_levels[index], deepest_plausible)
                         if visual_levels[index] else deepest_plausible)
        else:
            # The TOC's visual level is the primary evidence for unnumbered
            # headings. In particular, "Bibliography" is often a sibling of
            # numbered sections inside each chapter, not a new top-level item.
            unnumbered = True
            level = entry.level
            if not level:
                level = previous if chapter_open else fallback
            if (chapter_open and level <= chapter_level and previous > chapter_level
                    and not BOOK_BOUNDARY.match(title)):
                repeated = repeated_titles[clean_text(title).casefold()] > 1
                if (repeated or INTRINSIC_CHAPTER_END.match(title)
                        or _followed_by_chapter(result, index)):
                    level = chapter_level + 1
            if chapter_open and level <= chapter_level:
                chapter_open = False
                chapter_level = 0
                chapter_major = None
                active_numbered.clear()
            opens_heading = not chapter_open
        entry.level = max(1, min(level, previous + 1, 6))
        if opens_heading and (heading_level is None or entry.level <= heading_level):
            heading_level = entry.level
        if unnumbered:
            if ROMAN_HEADING.match(title):
                roman_level = entry.level
            elif roman_level is not None and entry.level <= roman_level:
                roman_level = None
        if parts and not roman_item:
            active_numbered[parts] = entry.level
        previous = entry.level
    return result


# Comparison keys of a bookmark that points at the printed contents page.
TOC_PAGE_TITLE_KEYS = frozenset({"目录", "目次", "contents", "tableofcontents"})


def toc_page_bookmark(entries: list[TocEntry], toc_page: int) -> TocEntry:
    """Return a first-level bookmark for the printed contents page itself.

    It is written before every recognized entry, titled in the language most
    of the recognized entries use.
    """
    chinese = sum(bool(re.search(r"[\u3400-\u9fff]", entry.title)) for entry in entries)
    title = "目录" if chinese * 2 >= len(entries) else "Contents"
    return TocEntry(title, 0, "toc", 1, toc_page, title, 1.0, pdf_page=toc_page)


def unique_entries(entries: Iterable[TocEntry]) -> list[TocEntry]:
    seen: set[tuple[str, int, str]] = set()
    output: list[TocEntry] = []
    for entry in entries:
        key = (re.sub(r"\W", "", entry.title).lower(), entry.printed_page, entry.numbering)
        if key not in seen:
            seen.add(key)
            output.append(entry)
    return output
