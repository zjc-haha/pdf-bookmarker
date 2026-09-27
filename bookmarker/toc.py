"""Printed table-of-contents detection and row parsing."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable

from .extract import Line, PageContent, _roman_to_int, clean_text


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


HEADING = re.compile(r"^(?:目\s*录|目\s*次|内\s*容\s*提\s*要|contents?|table\s+of\s+contents?)$", re.I)
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
ARABIC_PAGE_TOKEN = r"(?:[0-9][0-9\s]{0,5}|\(\s*[0-9][0-9\s]{0,5}\s*\))"
TRAILING = re.compile(
    rf"^(.*?)(?:[.．。…·•]{{2,}}|\s{{2,}}|\s*[/／]{{2}}\s*)\s*({ARABIC_PAGE_TOKEN}|[ivxlcdm]{{1,8}})\s*$",
    re.I,
)
PLAIN_TRAILING = re.compile(rf"^(.{{3,}}?)\s+({ARABIC_PAGE_TOKEN}|[ivxlcdm]{{1,8}})\s*$", re.I)
PAGE_TOKEN = re.compile(
    rf"^[\s.．。…·•,'‘’“”~—–-]*({ARABIC_PAGE_TOKEN}|[ivxlcdm]{{1,8}})[\s.．。…·•,'‘’“”~—–-]*$",
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


def _clean_title(text: str) -> str:
    text = clean_text(text)
    text = re.sub(r"[.．。…·•\s]+$", "", text)
    text = re.sub(r"^(?:[.．。…·•\s]+)", "", text)
    text = re.sub(r"(?<=\d)\s*[.．]\s*(?=\d)", ".", text)
    return text.strip(" -—–")


def parse_line(line: Line, width: float, source_page: int, method: str) -> TocEntry | None:
    if not line.text or HEADING.fullmatch(re.sub(r"\s+", "", line.text)):
        return None
    raw = line.text
    parsed_text = clean_text(raw)
    # Some PDF text layers split one printed page number into adjacent glyphs:
    # "2.6 Phasors ... 2 3" is page 23, not a title ending in 2 on page 3.
    if len(line.words) >= 2:
        first, last = line.words[-2:]
        gap = last.x - (first.x + first.width)
        if (re.fullmatch(r"[0-9]", first.text) and re.fullmatch(r"[0-9]", last.text)
                and -1 <= gap <= max(3.0, first.height * 0.4)):
            parsed_text = re.sub(r"(?<=\d)\s+(?=\d\s*$)", "", parsed_text)
    title: str | None = None
    label: tuple[str, int] | None = None

    # OCR often produces page numbers as separate blocks, but their x coordinate
    # still identifies the rightmost number on the same printed row.
    right = [w for w in line.words if w.x >= width * 0.68]
    left = [w for w in line.words if w.x < width * 0.68]
    if right and left:
        for length in (3, 2, 1):
            suffix = right[-length:]
            if len(suffix) != length:
                continue
            candidate = clean_text("".join(w.text for w in suffix))
            token = PAGE_TOKEN.fullmatch(candidate)
            if token:
                parsed = _page_value(token.group(1))
                if parsed:
                    suffix_ids = {id(word) for word in suffix}
                    title = " ".join(word.text for word in line.words
                                     if id(word) not in suffix_ids)
                    label = parsed
                    break

    if label is None:
        match = TRAILING.fullmatch(parsed_text)
        if match:
            title = match.group(1)
            label = _page_value(match.group(2))
    if label is None:
        match = PLAIN_TRAILING.fullmatch(parsed_text)
        if match:
            possible_title = _clean_title(match.group(1))
            if _title_level(possible_title) or re.fullmatch(r"(?:problems?|exercises?|solutions?|appendix|references?|bibliography)", possible_title, re.I):
                title = possible_title
                label = _page_value(match.group(2))
    if label is None or title is None:
        return None

    title = _clean_title(title)
    if len(re.sub(r"\W", "", title)) < 2 or len(title) > 140:
        return None
    if re.fullmatch(r"[\d\W]+", title):
        return None
    # A footer, isolated formula or body line should not masquerade as a TOC.
    if title.count("=") >= 2 or title.count("[") >= 2:
        return None
    confidence = 0.80 if method == "text" else 0.62
    if re.search(r"[.．。…]{2,}", raw):
        confidence += 0.10
    if _title_level(title):
        confidence += 0.06
    if "�" in title or "□" in title:
        confidence -= 0.25
    return TocEntry(title, label[1], label[0], _title_level(title), source_page, raw,
                    min(max(confidence, 0.0), 0.99))


def page_entries(content: PageContent) -> tuple[list[TocEntry], float]:
    entries: list[TocEntry] = []
    lines = content.lines
    index = 0
    while index < len(lines):
        line = lines[index]
        item = parse_line(line, content.width, content.page_number, content.method)
        # A wrapped numbered title can finish on the next printed row. The
        # continuation line may itself have a rightmost page number, so pair it
        # before treating it as an independent bookmark.
        if item is None and index + 1 < len(lines) and _title_level(line.text):
            following = lines[index + 1]
            same_column = abs(following.x - line.x) < content.width * 0.2
            nearby = 0 < following.y - line.y < max(28.0, (line.bottom - line.y) * 2.2)
            starts_new = bool(CHAPTER.match(following.text) or NUMBERED.match(following.text)
                              or SECTION.match(following.text))
            if same_column and nearby and not starts_new:
                combined = Line(line.text + " " + following.text, line.words + following.words,
                                min(line.x, following.x), line.y,
                                max(line.right, following.right), following.bottom)
                item = parse_line(combined, content.width, content.page_number, content.method)
                if item:
                    index += 1
        if item:
            entries.append(item)
        index += 1
    headings = sum(bool(HEADING.fullmatch(re.sub(r"\s+", "", line.text)))
                   for line in content.lines if line.y < content.height * 0.35)
    labels = [entry.printed_page for entry in entries if entry.numbering == "arabic"]
    progression = sum(b >= a for a, b in zip(labels, labels[1:])) / max(len(labels) - 1, 1)
    score = min(len(entries), 12) + headings * 4 + (2 if len(labels) >= 3 and progression >= 0.7 else 0)
    if content.method == "text-weak":
        score -= 2
    return entries, score


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
