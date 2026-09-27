"""Shared table-of-contents entries and bookmark hierarchy normalization."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Iterable

from .extract import _roman_to_int, clean_text


# Changing outline construction must invalidate successful --resume records.
# TOC prompt/cache revisions are tracked separately in deepseek.py.
HIERARCHY_VERSION = 10


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
NAMED_CHAPTER = re.compile(r"^(?:第\s*[一二三四五六七八九十百零〇0-9]+\s*章|chapters?\s+\d+)", re.I)
# "1.2", "§ 3" or "* 4.4": a starred row is an optional section.
NUMBERED = re.compile(
    r"^(?:[*＊]\s*)?(?:§\s*)?"
    r"(\d{1,3}(?:\s*[.．]\s*\d{1,3}){0,5})(?:\s*[.．、](?!\d))?(?![\d.．])\s*",
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


def _page_shift(entries: list[TocEntry], shifts: dict[int, int], index: int) -> int:
    """How far the model's levels on this row's page sit from the numbering.

    Rows whose level follows from their numbering show how the model's visual
    levels relate to the outline nearby.  A continuation page that starts
    inside a chapter is often read one level too shallow; the same shift then
    applies to its unnumbered rows.  The two nearest numbered rows above and
    the nearest below on the same page vote, so one misjudged row cannot move
    its neighbors; two rows that disagree leave the levels as reported.
    """
    page = entries[index].source_page
    before: list[int] = []
    for position in range(index - 1, -1, -1):
        if entries[position].source_page != page or len(before) == 2:
            break
        if position in shifts:
            before.append(shifts[position])
    after: list[int] = []
    for position in range(index + 1, len(entries)):
        if entries[position].source_page != page or after:
            break
        if position in shifts:
            after.append(shifts[position])
    votes = before + after
    if len(votes) == 3:
        return sorted(votes)[1]
    # Two rows that disagree give no shift to trust.
    return votes[0] if votes and len(set(votes)) == 1 else 0


def _assign_levels(entries: list[TocEntry], visual: list[int],
                   shifts: dict[int, int] | None) -> tuple[list[int], dict[int, int]]:
    """Give each row its outline level, numbering first.

    Numbered rows take their level from the numbering: "1.1" sits one level
    below "§1", "1" or "第1章", whatever indentation the model reported.
    Unnumbered rows ("习题", "Bibliography", "绪论") follow the printed
    indentation, shifted by ``_page_shift``.  ``shifts`` holds the shifts
    found by a previous pass; without it only rows above are known.
    """
    named_chapters = any(NAMED_CHAPTER.match(entry.title) for entry in entries)
    # In a book with "第二章" rows, bare "§1" or "1." rows are its sections
    # and "1.1" follows "§1", not the chapter number.
    section_rows = named_chapters and any(
        len(_numbered_parts(entry.title) or ()) == 1 for entry in entries)
    repeated_titles = _repeated_unnumbered_titles(entries)
    explicit = [level for level in visual if level]
    fallback = min(explicit) if explicit else 1
    found: dict[int, int] = {}
    levels: list[int] = []
    previous = 0
    # Open numbered rows, outermost first, as (numbering, level).
    numbered: list[tuple[tuple[int, ...], int]] = []
    part_open = False
    part_level = 0
    chapter_open = False
    chapter_level = 0
    chapter_number: int | None = None
    # Level of an open unnumbered heading such as "绪论", which owns the
    # "0.1" or "1." sections that follow it.
    heading_level: int | None = None
    # Level of the latest Roman-numbered heading, which owns the arabic
    # "1", "2" items after it (Born & Wolf's appendices I, II, III).
    roman_level: int | None = None
    # For each row, the numbering of the next part, chapter or numbered row.
    upcoming: list[tuple[int, ...] | None] = [None] * len(entries)
    following: tuple[int, ...] | None = None
    for index in range(len(entries) - 1, -1, -1):
        upcoming[index] = following
        title = entries[index].title
        if PART.match(title) or CHAPTER.match(title):
            following = None
        elif (parts := _numbered_parts(title)):
            following = parts
    for index, entry in enumerate(entries):
        title = entry.title
        parts = _numbered_parts(title)
        shift = _page_shift(entries, found if shifts is None else shifts, index)
        indent = max(1, visual[index] + shift) if visual[index] else 0
        key: tuple[int, ...] | None = None
        by_numbering = False
        opens_heading = False
        unnumbered = False
        if PART.match(title):
            level = visual[index] or 1
            part_open = True
            part_level = max(1, min(level, previous + 1, 6))
            chapter_open = False
            chapter_level = 0
            chapter_number = None
            heading_level = None
            roman_level = None
        elif APPENDIX.match(title) and chapter_open and indent > chapter_level:
            # "附录 1.1" indented like the chapter's sections belongs to that
            # chapter. Only an appendix at chapter level is a book division.
            level = indent
        elif CHAPTER.match(title):
            # A chapter is at the root, or visibly indented below a Part.
            level = (visual[index] if part_open and visual[index] > part_level else 1)
            by_numbering = not APPENDIX.match(title)
            chapter_open = True
            heading_level = None
            roman_level = None
            chapter_match = CHAPTER_NUMBER.match(title)
            chapter_number = (int(chapter_match.group(1) or chapter_match.group(2))
                              if chapter_match else None)
            chapter_level = max(1, min(level, previous + 1, 6))
            if chapter_number is not None:
                key = (chapter_number,)
        elif parts and len(parts) == 1:
            by_numbering = True
            if roman_level is not None:
                # "1 Euler's equations" under "I The Calculus of variations"
                # is an item of that heading, not an arabic-numbered chapter.
                level = roman_level + 1
            elif section_rows:
                owner = chapter_level if chapter_open else heading_level
                level = owner + 1 if owner is not None else 1
                key = parts
            else:
                chapter_open = True
                heading_level = None
                chapter_number = parts[0]
                level = (visual[index] if part_open and visual[index] > part_level else 1)
                chapter_level = max(1, min(level, previous + 1, 6))
                key = parts
        elif parts:
            by_numbering = True
            key = parts
            ancestor = next((parent_level for prefix, parent_level in reversed(numbered)
                             if len(prefix) < len(parts) and parts[:len(prefix)] == prefix),
                            None)
            if ancestor is None and chapter_open and not section_rows:
                if chapter_number is None:
                    chapter_number = parts[0]
                elif chapter_number != parts[0]:
                    # "6.1" after "Chapter 5" whose chapter row was not read.
                    chapter_open = False
                    chapter_level = 0
            if ancestor is not None:
                level = ancestor + 1
            elif chapter_open:
                level = chapter_level + 1
            elif heading_level is not None:
                level = heading_level + 1
            else:
                level = 1
        else:
            # Printed indentation places unnumbered rows. In particular,
            # "Bibliography" is often a sibling of numbered sections inside
            # each chapter, not a new top-level item.
            unnumbered = True
            level = indent or (previous if chapter_open else fallback)
            later = upcoming[index]
            if later and len(later) > 1:
                # A row between 5.1.1 and 5.1.2 stays inside 5.1.
                inside = next((parent_level for prefix, parent_level in reversed(numbered)
                               if len(prefix) < len(later)
                               and later[:len(prefix)] == prefix), None)
                if inside is not None:
                    level = max(level, inside + 1)
            if (chapter_open and level <= chapter_level and previous > chapter_level
                    and not BOOK_BOUNDARY.match(title)):
                repeated = repeated_titles[clean_text(title).casefold()] > 1
                if (repeated or INTRINSIC_CHAPTER_END.match(title)
                        or _followed_by_chapter(entries, index)):
                    level = chapter_level + 1
            if chapter_open and level <= chapter_level:
                chapter_open = False
                chapter_level = 0
                chapter_number = None
            opens_heading = not chapter_open
        level = max(1, min(level, previous + 1, 6))
        while numbered and numbered[-1][1] >= level:
            numbered.pop()
        if key is not None:
            numbered.append((key, level))
        if by_numbering and visual[index]:
            found[index] = level - visual[index]
        if opens_heading and (heading_level is None or level <= heading_level):
            heading_level = level
        if unnumbered:
            if ROMAN_HEADING.match(title):
                roman_level = level
            elif roman_level is not None and level <= roman_level:
                roman_level = None
        levels.append(level)
        previous = level
    return levels, found


def normalize_levels(entries: Iterable[TocEntry]) -> list[TocEntry]:
    result = list(entries)
    if not result:
        return result
    visual = [entry.level for entry in result]
    # The first pass finds how numbered rows sit against the model's levels;
    # the second also lets rows at the top of a page use the rows below them.
    _, shifts = _assign_levels(result, visual, None)
    levels, _ = _assign_levels(result, visual, shifts)
    for entry, level in zip(result, levels):
        entry.level = level
    return result


def _row_number(title: str) -> tuple[int, ...] | None:
    chapter = CHAPTER_NUMBER.match(title)
    if chapter:
        return (int(chapter.group(1) or chapter.group(2)),)
    return _numbered_parts(title)


def _follows(previous: tuple[int, ...], following: tuple[int, ...]) -> bool:
    """Whether ``following`` is the next number after ``previous``.

    3.4 is followed by 3.4.1, 3.5, 4 or 4.1; 3.4.2 also by 3.5.
    """
    if following[:len(previous)] == previous:
        return len(following) > len(previous) and all(
            part == 1 for part in following[len(previous):])
    for depth in range(min(len(previous), len(following)), 0, -1):
        if (following[:depth - 1] == previous[:depth - 1]
                and following[depth - 1] == previous[depth - 1] + 1
                and all(part == 1 for part in following[depth:])):
            return True
    return False


def rows_continue(before: list[TocEntry], after: list[TocEntry]) -> bool:
    """Whether contents rows on two pages follow on without a missing page.

    The last numbered row before and the first numbered row after must be
    consecutive numbers, and printed pages must not go backwards.
    """
    if not before or not after or after[0].printed_page < before[-1].printed_page:
        return False
    last = next((number for entry in reversed(before)
                 if (number := _row_number(entry.title))), None)
    first = next((number for entry in after if (number := _row_number(entry.title))), None)
    return last is not None and first is not None and _follows(last, first)


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
