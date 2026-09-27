"""Shared table-of-contents entries and bookmark hierarchy normalization."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable

from .extract import _roman_to_int, clean_text


# Changing outline construction must invalidate successful --resume records,
# while leaving the DeepSeek response cache (keyed by PROMPT_VERSION) reusable.
HIERARCHY_VERSION = 4


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


CHAPTER = re.compile(r"^(?:第\s*[一二三四五六七八九十百零〇0-9]+\s*[章篇部卷]|chapters?\s+\d+|附录|appendix)", re.I)
NUMBERED = re.compile(
    r"^(\d{1,3}(?:\s*[.．]\s*\d{1,3}){0,5})(?:\s*[.．、](?!\d))?(?![\d.．])\s*",
    re.I,
)
SECTION = re.compile(r"^(?:§|第\s*[一二三四五六七八九十百零〇0-9]+\s*节)", re.I)
CHAPTER_NUMBER = re.compile(r"^(?:第\s*(\d{1,3})\s*[章篇部卷]|chapters?\s+(\d{1,3}))", re.I)
CHAPTER_END = re.compile(
    r"^(?:本章小结|本章总结|本章习题|本章练习|小结|习题|练习题?|思考题|复习题|章末习题|"
    r"summary(?:\s|$)|exercises?(?:\s|$)|problems?(?:\s|$))",
    re.I,
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


def _title_level(title: str) -> int:
    if CHAPTER.match(title):
        return 1
    parts = _numbered_parts(title)
    if parts:
        return min(len(parts), 6)
    if SECTION.match(title):
        return 2
    if re.match(r"^(?:参考文献|索引|后记|references?|bibliography|index)\b", title, re.I):
        return 1
    return 0


def _numbered_parts(title: str) -> tuple[int, ...] | None:
    match = NUMBERED.match(title)
    if not match:
        return None
    return tuple(int(part) for part in re.split(r"\s*[.．]\s*", match.group(1)))


def normalize_levels(entries: Iterable[TocEntry]) -> list[TocEntry]:
    result = list(entries)
    if not result:
        return result
    explicit = [item.level for item in result if item.level]
    fallback = min(explicit) if explicit else 1
    previous = 0
    active_numbered: dict[tuple[int, ...], int] = {}
    chapter_open = False
    chapter_major: int | None = None
    for entry in result:
        title = entry.title
        parts = _numbered_parts(title)
        if CHAPTER.match(title):
            chapter_open = True
            active_numbered.clear()
            chapter_match = CHAPTER_NUMBER.match(title)
            chapter_major = (int(chapter_match.group(1) or chapter_match.group(2))
                             if chapter_match else None)
            if chapter_major is not None:
                active_numbered[(chapter_major,)] = 1
            level = 1
        elif parts:
            if len(parts) == 1:
                active_numbered.clear()
                chapter_open = True
                chapter_major = parts[0]
                level = 1
            else:
                if chapter_major is not None and parts[0] != chapter_major:
                    active_numbered.clear()
                    chapter_open = False
                if chapter_major != parts[0]:
                    chapter_major = parts[0]
                    if chapter_open:
                        active_numbered[(parts[0],)] = 1
                active_numbered = {
                    prefix: parent_level for prefix, parent_level in active_numbered.items()
                    if parts[:len(prefix)] == prefix
                }
                parent = active_numbered.get(parts[:-1])
                level = (parent + 1 if parent is not None else
                         2 if chapter_open and (parts[0],) in active_numbered else 1)
        else:
            level = _title_level(title)
            if level == 1:
                chapter_open = False
                chapter_major = None
                active_numbered.clear()
            elif not level and chapter_open and CHAPTER_END.match(title):
                level = 2
            elif not level:
                level = entry.level
                if level == 0:
                    level = previous if title.startswith(("附", "参")) else fallback
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
