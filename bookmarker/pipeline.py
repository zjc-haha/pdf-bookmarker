"""Conservative single-book analysis and bookmark export."""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter

from .extract import clean_text
from .toc import TocEntry


@dataclass
class Anchor:
    pdf_page: int
    printed_page: int
    numbering: str
    offset: int
    source: str


@dataclass
class BookResult:
    source: str
    status: str
    page_count: int = 0
    existing_bookmarks: int = 0
    existing_outline_quality: float = 0.0
    existing_outline_check: dict[str, Any] = field(default_factory=dict)
    toc_pages: list[int] = field(default_factory=list)
    toc_corrections: list[dict[str, Any]] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    anchors: list[dict[str, Any]] = field(default_factory=list)
    offsets: dict[str, int] = field(default_factory=dict)
    offset_segments: list[dict[str, Any]] = field(default_factory=list)
    output: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _outline_count(items: list) -> int:
    return sum(_outline_count(item) if isinstance(item, list) else 1 for item in items)


def _outline_titles(items: list) -> list[str]:
    titles: list[str] = []
    for item in items:
        if isinstance(item, list):
            titles.extend(_outline_titles(item))
        else:
            titles.append(str(item.get("/Title", "")))
    return titles


def _is_page_label_title(title: str) -> bool:
    """Recognize outline entries that only repeat a printed page label."""
    compact = re.sub(r"\s+", "", clean_text(title).strip("()[]{}【】（）<>《》"))
    if not compact:
        return True
    chinese_number = r"[一二三四五六七八九十百千万零〇○两]+"
    number = rf"(?:\d+|{chinese_number})"
    return bool(
        re.fullmatch(r"[\divxlcdm.·…\-–—_/]+", compact, re.I)
        or re.fullmatch(chinese_number, compact)
        or re.fullmatch(rf"第?{number}[页頁](?:[/／]共?{number}[页頁])?", compact)
        or re.fullmatch(r"(?:pages?|pp?)\.?(?:no\.?)?\d+(?:of\d+)?", compact, re.I)
    )


def _outline_page_label_ratio(items: list) -> float:
    """Share of existing bookmarks that are page-number placeholders."""
    titles = _outline_titles(items)
    return sum(_is_page_label_title(title) for title in titles) / len(titles) if titles else 0.0


def _outline_quality(items: list) -> float:
    titles = _outline_titles(items)
    if not titles:
        return 0.0
    useful = 0
    for title in titles:
        title = clean_text(title)
        if _is_page_label_title(title):
            continue
        if re.search(r"\.(?:pdf|djvu)$", title, re.I):
            continue
        key = _title_key(title)
        if len(key) >= 3 or len(re.findall(r"[\u3400-\u9fff]", key)) >= 2:
            useful += 1
    return useful / len(titles)


def _outline_action(bookmark_count: int, page_count: int, quality: float,
                    *, skip_existing: bool = True) -> str:
    """Return how processing treats the old outline after a successful analysis.

    A full useful outline is skipped by default. A sparse useful outline is
    preserved alongside a nested generated TOC. Page-label placeholders are
    replaced, so they cannot swamp the generated chapter bookmarks.
    """
    if not skip_existing:
        return "replace"
    if bookmark_count >= max(30, page_count // 10) and quality >= 0.55:
        return "skip"
    if bookmark_count and quality >= 0.55:
        return "preserve"
    return "replace"


def _outline_compare_key(title: str) -> str:
    """Ignore typography but retain chapter and section numbers for comparison."""
    normalized = clean_text(title).casefold()
    # A period between digits separates section numbers; other dots are
    # punctuation or dotted leaders from the printed table of contents.
    normalized = re.sub(r"(?<!\d)\.|\.(?!\d)", "", normalized)
    return re.sub(r"[^a-z0-9\u3400-\u9fff.]", "", normalized)


def _outline_signature(reader: PdfReader) -> list[tuple[str, int | None, int]]:
    """Read title, destination and nesting level from the existing outline."""
    signature: list[tuple[str, int | None, int]] = []

    def visit(items: list, level: int) -> None:
        for item in items:
            if isinstance(item, list):
                visit(item, level + 1)
                continue
            try:
                title = str(item.get("/Title", ""))
            except (AttributeError, TypeError, ValueError):
                title = str(item)
            try:
                index = reader.get_destination_page_number(item)
                page = index + 1 if isinstance(index, int) and index >= 0 else None
            except Exception:
                page = None
            signature.append((_outline_compare_key(title), page, level))

    visit(reader.outline, 1)
    return signature


def _compare_existing_outline(reader: PdfReader, entries: list[TocEntry]) -> dict[str, Any]:
    """Compare old bookmarks to a trusted, page-mapped printed TOC.

    A title-only count can call an incomplete or misplaced outline complete.
    The verified mode requires the same ordered titles, PDF destinations, and
    nesting levels.  Extra cover or publisher bookmarks therefore also count
    as differences and are replaced by the printed TOC.
    """
    actual = _outline_signature(reader)
    expected = [(_outline_compare_key(entry.title), entry.pdf_page, entry.level)
                for entry in entries]
    old_titles = Counter(title for title, _, _ in actual)
    new_titles = Counter(title for title, _, _ in expected)
    missing = sum((new_titles - old_titles).values())
    unexpected = sum((old_titles - new_titles).values())
    common = set(old_titles) & set(new_titles)
    wrong_pages = 0
    wrong_levels = 0
    for title in common:
        old = [(page, level) for key, page, level in actual if key == title]
        new = [(page, level) for key, page, level in expected if key == title]
        for (old_page, old_level), (new_page, new_level) in zip(old, new):
            wrong_pages += old_page != new_page
            wrong_levels += old_level != new_level
    return {
        "matches": actual == expected,
        "expected": len(expected),
        "existing": len(actual),
        "matched_titles": sum((old_titles & new_titles).values()),
        "missing_titles": missing,
        "unexpected_titles": unexpected,
        "wrong_pages": wrong_pages,
        "wrong_levels": wrong_levels,
        "wrong_order": actual != expected and not any(
            (missing, unexpected, wrong_pages, wrong_levels)),
    }


def _in_place_replacement_review_reason(
    source: Path, output: Path, check: dict[str, Any], quality: float,
    *, skip_existing: bool,
) -> str | None:
    """Hold an in-place rewrite when a useful old outline barely matches the TOC."""
    if (not skip_existing or source.resolve() != output.resolve()
            or quality < 0.55 or check.get("matches")):
        return None
    expected = int(check.get("expected", 0))
    existing = int(check.get("existing", 0))
    common = int(check.get("matched_titles", expected - int(check.get("missing_titles", expected))))
    if expected < 4 or existing < max(4, expected // 2) or common * 5 >= expected:
        return None
    return (f"现有书签质量较高，但与印刷目录仅有 {common}/{expected} 个标题匹配；"
            "为避免直接覆盖原 PDF，已暂停此书。请核对目录，确认后可勾选“强制替换已有书签”重试")


def _sample_body_pages(toc_pages: list[int], page_count: int) -> list[int]:
    if toc_pages[0] > page_count * 0.70:
        first = list(range(1, min(13, page_count + 1)))
        spread = [round(page_count * fraction) for fraction in (0.2, 0.4, 0.6, 0.8)]
    else:
        start = min(toc_pages[-1] + 1, page_count)
        first = list(range(start, min(start + 11, page_count + 1)))
        span = max(page_count - start, 1)
        spread = [start + round(span * fraction) for fraction in (0.2, 0.4, 0.6, 0.8, 0.97)]
    return sorted({number for number in first + spread if 1 <= number <= page_count
                   and number not in toc_pages})


def _title_key(text: str) -> str:
    text = clean_text(text).lower()
    text = re.sub(r"^(?:第\s*[一二三四五六七八九十百零〇0-9]+\s*[章节篇部卷]|\d+(?:\.\d+)*|§\s*\d+)", "", text)
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", text)


def _title_anchors(pdf_path: Path, toc_pages: list[int], entries: list[TocEntry]) -> list[Anchor]:
    if not entries:
        return []
    try:
        with pdfium.PdfDocument(str(pdf_path)) as document:
            normalized = []
            for page in document:
                text_page = page.get_textpage()
                try:
                    normalized.append(_title_key(text_page.get_text_bounded()))
                finally:
                    text_page.close()
                    page.close()
    except (OSError, ValueError, pdfium.PdfiumError):
        return []
    anchors: list[Anchor] = []
    used_titles: set[str] = set()
    for entry in entries:
        if entry.numbering != "arabic":
            continue
        key = _title_key(entry.title)
        if len(key) < 7 or key in used_titles:
            continue
        used_titles.add(key)
        matches = [i + 1 for i in range(len(normalized))
                   if i + 1 not in toc_pages and key in normalized[i]]
        if len(matches) == 1:
            pdf_page = matches[0]
            anchors.append(Anchor(pdf_page, entry.printed_page, entry.numbering,
                                  pdf_page - entry.printed_page, "chapter-title"))
        if len(anchors) >= 8:
            break
    return anchors


def _collect_anchors(pdf, labels: Any, toc_pages: list[int], entries: list[TocEntry]) -> list[Anchor]:
    anchors: list[Anchor] = []
    for pdf_page in _sample_body_pages(toc_pages, len(pdf.pages)):
        for numbering, printed in labels.footer_labels(pdf, pdf_page):
            if 0 < printed <= len(pdf.pages) + 200:
                anchors.append(Anchor(pdf_page, printed, numbering, pdf_page - printed, "page-label"))
    counts = Counter((anchor.numbering, anchor.offset) for anchor in anchors)
    if not counts or max(counts.values()) < 3:
        anchors.extend(_title_anchors(labels.pdf_path, toc_pages, entries))
    return anchors


def _outline_page_anchors(reader: PdfReader, toc_pages: list[int]) -> list[Anchor]:
    """Select five structurally consistent old page-number bookmarks.

    Old outlines are untrusted. This only finds a plausible mapping; the
    printed labels on the destination pages must still confirm it below.
    """
    outline = reader.outline
    total = _outline_count(outline)
    page_count = len(reader.pages)
    if not total or not page_count:
        return []
    candidates: list[Anchor] = []

    def visit(items: list) -> bool:
        for item in items:
            if isinstance(item, list):
                if not visit(item):
                    return False
                continue
            title = clean_text(str(item.get("/Title", "")))
            # Leading zeroes, decimal section numbers, and roman front matter
            # are not evidence for an Arabic page-number offset.
            if not re.fullmatch(r"[1-9]\d{0,4}", title):
                continue
            printed = int(title)
            try:
                index = reader.get_destination_page_number(item)
            except Exception:
                return False
            if not isinstance(index, int) or not 0 <= index < page_count:
                return False
            candidates.append(Anchor(index + 1, printed, "arabic", index + 1 - printed,
                                     "existing-bookmark"))
        return True

    if not visit(outline):
        return []
    if len(candidates) < max(12, (page_count + 9) // 10):
        return []
    if len(candidates) / total < 0.70:
        return []
    if len({item.printed_page for item in candidates}) != len(candidates):
        return []
    if len({item.pdf_page for item in candidates}) != len(candidates):
        return []
    if any(right.printed_page <= left.printed_page or right.pdf_page <= left.pdf_page
           for left, right in zip(candidates, candidates[1:])):
        return []
    if any(item.pdf_page in toc_pages for item in candidates):
        return []
    best_offset, support = Counter(item.offset for item in candidates).most_common(1)[0]
    if support * 20 < len(candidates) * 19:  # At least 95% share the same offset.
        return []
    consistent = [item for item in candidates if item.offset == best_offset]
    if consistent[-1].pdf_page - consistent[0].pdf_page + 1 < max(12, (page_count + 4) // 5):
        return []
    longest_run = run = 1
    for left, right in zip(consistent, consistent[1:]):
        run = run + 1 if (right.printed_page == left.printed_page + 1
                          and right.pdf_page == left.pdf_page + 1) else 1
        longest_run = max(longest_run, run)
    if longest_run < max(10, len(consistent) // 4):
        return []
    # Prefer pages used by the normal body sample. A previous run can then
    # reuse its validated page-label cache without sending those images again.
    if toc_pages[0] > page_count * 0.70:
        targets = [10] + [round(page_count * fraction)
                          for fraction in (0.2, 0.4, 0.6, 0.8)]
    else:
        start = min(toc_pages[-1] + 1, page_count)
        span = max(page_count - start, 1)
        targets = [min(start + 9, page_count)] + [
            start + round(span * fraction) for fraction in (0.2, 0.4, 0.6, 0.97)]
    selected: list[Anchor] = []
    for target in targets:
        available = [item for item in consistent if item not in selected]
        if not available:
            break
        selected.append(min(available, key=lambda item: abs(item.pdf_page - target)))
    return sorted(selected, key=lambda item: item.pdf_page)


def _verified_outline_page_anchors(reader: PdfReader, pdf, labels,
                                   toc_pages: list[int]) -> list[Anchor]:
    """Cross-check old numeric bookmarks against real printed page labels."""
    anchors = _outline_page_anchors(reader, toc_pages)
    if not anchors:
        return []
    prefetch = getattr(labels, "prefetch", None)
    if callable(prefetch):
        prefetch(pdf, [item.pdf_page for item in anchors])
    verified: list[Anchor] = []
    for item in anchors:
        observed = labels.footer_labels(pdf, item.pdf_page)
        expected = ("arabic", item.printed_page)
        if observed and any(label != expected for label in observed):
            return []
        if expected in observed:
            verified.append(Anchor(item.pdf_page, item.printed_page, "arabic",
                                   item.offset, "existing-bookmark-verified"))
    if len(verified) < 3:
        return []
    if verified[-1].pdf_page - verified[0].pdf_page < max(
            5, (anchors[-1].pdf_page - anchors[0].pdf_page) // 2):
        return []
    return verified


def _refine_boundary(pdf, labels: Any, left: int, right: int,
                     earlier_offset: int, later_offset: int, numbering: str,
                     anchors: list[Anchor]) -> tuple[int, int]:
    """Narrow a detected page-offset change using body page numbers."""
    while right - left > 2:
        midpoint = (left + right) // 2
        found = False
        for page_number in (midpoint, midpoint + 1, midpoint - 1):
            if not left < page_number < right:
                continue
            for label_type, printed in labels.footer_labels(pdf, page_number):
                if label_type != numbering:
                    continue
                offset = page_number - printed
                if offset in {earlier_offset, later_offset}:
                    anchors.append(Anchor(page_number, printed, numbering, offset, "boundary-check"))
                    if offset == earlier_offset:
                        left = page_number
                    else:
                        right = page_number
                    found = True
                    break
            if found:
                break
        if not found:
            break
    return left, right


def _fit_offsets(anchors: list[Anchor], pdf, labels: Any,
                 required_numberings: set[str]
                 ) -> tuple[dict[str, int], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    offsets: dict[str, int] = {}
    segments: list[dict[str, Any]] = []
    for numbering in sorted(required_numberings):
        relevant = [anchor for anchor in anchors if anchor.numbering == numbering]
        votes = Counter({offset: len({anchor.pdf_page for anchor in relevant if anchor.offset == offset})
                         for offset in {anchor.offset for anchor in relevant}})
        if not votes:
            continue
        ranked = votes.most_common()
        best_offset, best_votes = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0
        if best_votes < 3 or best_votes <= second:
            warnings.append(f"{numbering} 页码锚点不足或互相矛盾")
            continue
        offsets[numbering] = best_offset
        # A page can yield both its printed label and an unrelated heading
        # number. Such a conflicting reading is not
        # independent evidence of another offset segment.
        best_pages_set = {anchor.pdf_page for anchor in relevant if anchor.offset == best_offset}
        relevant = [anchor for anchor in relevant
                    if anchor.offset == best_offset or anchor.pdf_page not in best_pages_set]
        votes = Counter({offset: len({anchor.pdf_page for anchor in relevant if anchor.offset == offset})
                         for offset in {anchor.offset for anchor in relevant}})
        ranked = votes.most_common()
        second = ranked[1][1] if len(ranked) > 1 else 0
        if second < 2:
            segments.append({"numbering": numbering, "offset": best_offset,
                             "from_printed": 1, "votes": best_votes})
            # A conflicting sampled page can represent a real missing/extra page.
            if second == 1:
                other_offset = ranked[1][0]
                other_pages = [a.pdf_page for a in relevant if a.offset == other_offset]
                main_pages = [a.pdf_page for a in relevant if a.offset == best_offset]
                if other_pages and (min(other_pages) > max(main_pages) or max(other_pages) < min(main_pages)):
                    warnings.append(f"{numbering} 页码在书籍边缘可能发生偏移变化")
            continue
        if len(ranked) > 2 and ranked[2][1] >= 2:
            warnings.append(f"{numbering} 页码出现三个以上偏移值")
            continue
        other_offset = ranked[1][0]
        best_pages = sorted({a.pdf_page for a in relevant if a.offset == best_offset})
        other_pages = sorted({a.pdf_page for a in relevant if a.offset == other_offset})
        ordered = [(best_offset, best_pages), (other_offset, other_pages)]
        ordered.sort(key=lambda pair: statistics.median(pair[1]))
        (earlier_offset, earlier_pages), (later_offset, later_pages) = ordered
        left, right = max(earlier_pages), min(later_pages)
        if left >= right or min(later_pages) <= min(earlier_pages):
            warnings.append(f"{numbering} 页码偏移锚点交错，无法确定分段")
            continue
        left, right = _refine_boundary(pdf, labels, left, right, earlier_offset,
                                       later_offset, numbering, anchors)
        if right - left > 3:
            warnings.append(f"{numbering} 页码偏移变化位置仍有 {right - left} 页不确定")
            continue
        first_printed = right - later_offset
        segments.extend([
            {"numbering": numbering, "offset": earlier_offset, "from_printed": 1,
             "until_printed": first_printed - 1, "votes": len(earlier_pages)},
            {"numbering": numbering, "offset": later_offset, "from_printed": first_printed,
             "votes": len(later_pages)},
        ])
    return offsets, segments, warnings


def _map_entries(entries: list[TocEntry], offsets: dict[str, int],
                 segments: list[dict[str, Any]], page_count: int) -> list[str]:
    warnings: list[str] = []
    for entry in entries:
        applicable = [segment for segment in segments
                      if segment["numbering"] == entry.numbering
                      and entry.printed_page >= segment["from_printed"]
                      and entry.printed_page <= segment.get("until_printed", 10**9)]
        offset = applicable[0]["offset"] if len(applicable) == 1 else offsets.get(entry.numbering)
        if offset is None:
            warnings.append(f"缺少 {entry.numbering} 页码映射")
            continue
        target = entry.printed_page + offset
        if 1 <= target <= page_count:
            entry.pdf_page = target
        else:
            warnings.append(f"书签目标越界：{entry.title} → PDF 第 {target} 页")
    mapped = [entry.pdf_page for entry in entries if entry.pdf_page is not None]
    if sum(b < a for a, b in zip(mapped, mapped[1:])) > 1:
        warnings.append("书签目标页出现明显倒退")
    return list(dict.fromkeys(warnings))


def _write_pdf(source: Path, destination: Path, entries: list[TocEntry],
               *, preserve_existing: bool = False) -> None:
    reader = PdfReader(source, strict=False)
    writer = PdfWriter()
    writer.append(reader, import_outline=preserve_existing)
    if reader.metadata:
        writer.add_metadata({str(key): str(value) for key, value in reader.metadata.items()
                             if value is not None})
    parents: dict[int, Any] = {}
    previous = 1
    root = writer.add_outline_item("自动识别目录", 0) if preserve_existing else None
    for entry in entries:
        if entry.pdf_page is None:
            raise ValueError(f"Unmapped bookmark: {entry.title}")
        level = max(1, min(entry.level, previous + 1))
        parent = parents.get(level - 1) if level > 1 else root
        if level > 1 and parent is None:
            level = 1
            parent = root
        outline = writer.add_outline_item(entry.title, entry.pdf_page - 1, parent=parent)
        parents[level] = outline
        for stale in [key for key in parents if key > level]:
            del parents[stale]
        previous = level
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    try:
        with temporary.open("wb") as stream:
            writer.write(stream)
        check = PdfReader(temporary, strict=False)
        expected = len(entries) + (_outline_count(reader.outline) + 1 if preserve_existing else 0)
        if len(check.pages) != len(reader.pages) or _outline_count(check.outline) != expected:
            raise RuntimeError("Written PDF failed page/bookmark verification")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
