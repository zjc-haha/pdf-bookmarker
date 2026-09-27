"""Shared PDF text and page-label helpers for DeepSeek bookmark processing."""

from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    text: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Line:
    text: str
    words: tuple[Word, ...]
    x: float
    y: float
    right: float
    bottom: float


@dataclass(frozen=True)
class PageContent:
    page_number: int
    width: float
    height: float
    lines: tuple[Line, ...]
    method: str


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("⋯", "…").replace("·", ".").replace("•", ".")
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    text = re.sub(r"\s+([，。；：、.])", r"\1", text)
    text = re.sub(r"([.。])\s+(?=[.。])", r"\1", text)
    return text.strip()


def _group_words(words: list[Word], width: float, height: float) -> tuple[Line, ...]:
    if not words:
        return ()

    # A distant right-hand page number does not count as column text.
    left_letters = sum(len(re.sub(r"[\W\d_]", "", w.text)) for w in words if w.x < width * 0.45)
    right_letters = sum(len(re.sub(r"[\W\d_]", "", w.text)) for w in words if w.x > width * 0.55)
    middle_letters = sum(len(re.sub(r"[\W\d_]", "", w.text)) for w in words if width * 0.45 <= w.x <= width * 0.55)
    two_columns = left_letters >= 18 and right_letters >= 18 and middle_letters < min(left_letters, right_letters) * 0.23

    columns: list[list[Word]]
    if two_columns:
        midpoint = width / 2
        columns = [[w for w in words if w.x < midpoint], [w for w in words if w.x >= midpoint]]
    else:
        columns = [words]

    output: list[Line] = []
    for column in columns:
        if not column:
            continue
        median_height = statistics.median(max(w.height, 1.0) for w in column)
        tolerance = max(3.0, median_height * 0.60)
        groups: list[list[Word]] = []
        for word in sorted(column, key=lambda w: (w.y + w.height * 0.5, w.x)):
            center = word.y + word.height * 0.5
            if groups:
                last_center = statistics.median(w.y + w.height * 0.5 for w in groups[-1])
                if abs(center - last_center) <= tolerance:
                    groups[-1].append(word)
                    continue
            groups.append([word])
        for group in groups:
            group.sort(key=lambda w: w.x)
            parts: list[str] = []
            previous: Word | None = None
            for word in group:
                if previous is not None:
                    gap = word.x - (previous.x + previous.width)
                    parts.append("   " if gap > median_height * 1.7 else " ")
                parts.append(word.text)
                previous = word
            output.append(Line(
                text=clean_text("".join(parts)),
                words=tuple(group),
                x=min(w.x for w in group),
                y=min(w.y for w in group),
                right=max(w.x + w.width for w in group),
                bottom=max(w.y + w.height for w in group),
            ))
    return tuple(output)


def _pdfplumber_words(page) -> list[Word]:
    result: list[Word] = []
    for item in page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False):
        text = item.get("text", "").strip()
        if text:
            result.append(Word(text, float(item["x0"]), float(item["top"]),
                               float(item["x1"] - item["x0"]), float(item["bottom"] - item["top"])))
    return result


_ARABIC_LABEL = re.compile(r"^[\s\[(（—–-]*([0-9]{1,4})[\s\])）—–-]*$")
_ROMAN_LABEL = re.compile(r"^[\s\[(（—–-]*([ivxlcdm]{1,8})[\s\])）—–-]*$", re.I)


def _roman_to_int(text: str) -> int:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = previous = 0
    for character in reversed(text.lower()):
        current = values[character]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total


def _parse_page_label(text: str) -> tuple[str, int] | None:
    text = clean_text(text)
    match = _ARABIC_LABEL.fullmatch(text)
    if match:
        value = int(match.group(1))
        return ("arabic", value) if value > 0 else None
    match = _ROMAN_LABEL.fullmatch(text)
    if match:
        return ("roman", _roman_to_int(match.group(1)))
    return None


def _labels_in_edges(content: PageContent) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for line in content.lines:
        if line.y > content.height * 0.16 and line.bottom < content.height * 0.84:
            continue
        label = _parse_page_label(line.text)
        if label:
            found.append(label)
            continue
        if line.words:
            edge_tokens = (line.words[0], line.words[-1])
            for word in edge_tokens:
                if word.x < content.width * 0.2 or word.x > content.width * 0.78:
                    label = _parse_page_label(word.text)
                    if label:
                        found.append(label)
    return list(dict.fromkeys(found))
