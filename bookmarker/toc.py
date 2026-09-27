"""Shared table-of-contents entries and bookmark hierarchy normalization."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Iterable

from .extract import _roman_to_int, clean_text


# Changing outline construction must invalidate successful --resume records.
# TOC prompt/cache revisions are tracked separately in deepseek.py.
HIERARCHY_VERSION = 5


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


def normalize_levels(entries: Iterable[TocEntry]) -> list[TocEntry]:
    result = list(entries)
    if not result:
        return result
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
    for index, entry in enumerate(result):
        title = entry.title
        parts = _numbered_parts(title)
        if PART.match(title):
            level = entry.level or 1
            part_open = True
            part_level = max(1, min(level, previous + 1, 6))
            chapter_open = False
            chapter_level = 0
            chapter_major = None
            active_numbered.clear()
        elif CHAPTER.match(title):
            # A chapter is usually at the root, but can be visibly indented
            # below a Part. Numbering alone does not decide its level.
            level = (entry.level if part_open and entry.level > part_level else 1)
            chapter_open = True
            active_numbered.clear()
            chapter_match = CHAPTER_NUMBER.match(title)
            chapter_major = (int(chapter_match.group(1) or chapter_match.group(2))
                             if chapter_match else None)
            chapter_level = max(1, min(level, previous + 1, 6))
            if chapter_major is not None:
                active_numbered[(chapter_major,)] = chapter_level
        elif parts:
            if len(parts) == 1:
                active_numbered.clear()
                chapter_open = True
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
        entry.level = max(1, min(level, previous + 1, 6))
        if parts:
            active_numbered[parts] = entry.level
        previous = entry.level
    return result


def unique_entries(entries: Iterable[TocEntry]) -> list[TocEntry]:
    seen: set[tuple[str, int, str]] = set()
    output: list[TocEntry] = []
    for entry in entries:
        key = (re.sub(r"\W", "", entry.title).lower(), entry.printed_page, entry.numbering)
        if key not in seen:
            seen.add(key)
            output.append(entry)
    return output
