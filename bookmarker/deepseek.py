"""Vision based table-of-contents recognition using DeepSeek Flash.

The model reads rendered PDF pages. Deterministic page-offset fitting and
the PDF writer remain responsible
for deciding whether an outline is safe to export.
"""

from __future__ import annotations

import base64
import io
import json
import math
import re
import time
import urllib.error
import urllib.request
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import pdfplumber
import pypdfium2 as pdfium
from pdfminer.pdfexceptions import PDFException
from pdfplumber.utils.exceptions import PdfminerException
from PIL import Image
from pypdf import PdfReader

from .extract import PageContent, _group_words, _labels_in_edges, _pdfplumber_words
from .pipeline import (BookResult, _collect_anchors, _compare_existing_outline, _fit_offsets,
                       _in_place_replacement_review_reason, _map_entries, _outline_action,
                       _outline_count, _outline_quality,
                       _sample_body_pages, _verified_outline_page_anchors, _write_pdf)
from .toc import (TocEntry, _arabic_page_digits, _page_value,
                  normalize_levels, toc_page_bookmark, unique_entries)


DEEPSEEK_MODEL = "deepseek-flash"
PROMPT_VERSION = "vision-1"
TOC_PROMPT_VERSION = "layout-2"
API_URL = "https://api.deepseek.com/chat/completions"


class DeepSeekError(RuntimeError):
    """A request or response was unusable without exposing the API key."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        # Authorization must never be copied to a redirect destination.
        return None


class DeepSeekClient:
    def __init__(self, api_key: str, *, url: str = API_URL,
                 opener: Callable[..., Any] | None = None) -> None:
        if not api_key.strip():
            raise ValueError("请先设置 DEEPSEEK_API_KEY，或在图形界面输入 DeepSeek API Key")
        self._api_key = api_key.strip()
        self._url = url
        self._opener = opener or urllib.request.build_opener(_NoRedirect()).open
        # Only completed network responses count. Local recognition-cache hits
        # never call ask_json and therefore never add historical token usage.
        self.api_usage = {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0, "reported_responses": 0,
                          "unreported_responses": 0}

    def _record_usage(self, answer: Any) -> None:
        usage = answer.get("usage") if isinstance(answer, dict) else None
        if not isinstance(usage, dict):
            self.api_usage["unreported_responses"] += 1
            return
        values = [usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")]
        if (any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in values) or values[0] + values[1] != values[2]):
            self.api_usage["unreported_responses"] += 1
            return
        for key, value in zip(("prompt_tokens", "completion_tokens", "total_tokens"), values):
            self.api_usage[key] += value
        self.api_usage["reported_responses"] += 1

    def ask_json(self, prompt: str, images: list[tuple[int, Image.Image]], *,
                 max_tokens: int = 4096) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for page_number, image in images:
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="PNG", optimize=True)
            if buffer.tell() > 32 * 1024 * 1024:
                raise DeepSeekError(f"PDF 第 {page_number} 页图片超过 DeepSeek 单图限制")
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            content.extend([
                {"type": "text", "text": f"下面是 PDF 的第 {page_number} 页（从 1 开始计数）："},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + encoded, "detail": "original"}},
            ])
        payload = json.dumps({
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": max_tokens,
        }, ensure_ascii=False).encode("utf-8")
        if len(payload) > 48 * 1024 * 1024:
            raise DeepSeekError("图片请求超过 DeepSeek 48 MiB 限制")
        request = urllib.request.Request(
            self._url, data=payload, method="POST",
            headers={"Authorization": "Bearer " + self._api_key,
                     "Content-Type": "application/json", "User-Agent": "pdf-bookmarker/2"},
        )
        for attempt in range(3):
            try:
                with self._opener(request, timeout=120) as response:
                    try:
                        answer = json.load(response)
                    except (ValueError, TypeError):
                        self.api_usage["unreported_responses"] += 1
                        raise DeepSeekError("DeepSeek 未返回可解析的响应") from None
                break
            except urllib.error.HTTPError as error:
                if error.code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise DeepSeekError(f"DeepSeek HTTP {error.code}；请检查 API Key、额度或稍后重试") from None
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise DeepSeekError(f"DeepSeek 请求失败：{type(error).__name__}") from None
        self._record_usage(answer)
        try:
            choice = answer["choices"][0]
            if choice.get("finish_reason") == "length":
                raise DeepSeekError("DeepSeek 输出被截断；本书需要人工复核")
            raw = choice["message"]["content"]
            if not isinstance(raw, str) or not raw.strip():
                raise DeepSeekError("DeepSeek 返回了空结果")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return value
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise DeepSeekError("DeepSeek 未返回可解析的 JSON 对象") from error


class PageRenderer:
    def __init__(self, pdf_path: Path) -> None:
        self.pdf_path = Path(pdf_path)

    def render(self, page_number: int, *, scale: int = 1700) -> Image.Image:
        """Render a 1-based page into a detached RGB image bounded by ``scale``."""
        if isinstance(page_number, bool) or not isinstance(page_number, int):
            raise ValueError("页码必须是整数")
        if isinstance(scale, bool) or not isinstance(scale, int) or not 1 <= scale <= 2400:
            raise ValueError("scale 必须在 1 到 2400 之间")

        with pdfium.PdfDocument(str(self.pdf_path)) as pdf:
            if not 1 <= page_number <= len(pdf):
                raise ValueError(f"页码超出范围：应在 1 到 {len(pdf)} 之间")
            page = pdf[page_number - 1]
            try:
                width, height = page.get_size()
                if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                    raise RuntimeError(f"PDF 第 {page_number} 页尺寸无效")
                bitmap = page.render(scale=scale / max(width, height))
                try:
                    # to_pil() may share native bitmap memory. Convert while it
                    # is alive so the returned image owns its RGB pixels.
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
        if max(image.size) > scale:
            image.thumbnail((scale, scale), Image.Resampling.LANCZOS)
        return image


def _cached(cache_dir: Path, name: str, factory: Callable[[], dict[str, Any]],
            validate: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    path = cache_dir / (PROMPT_VERSION + "-" + DEEPSEEK_MODEL + "-" + name + ".json")
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Cached response is not an object")
            validate(value)
            return value
        except (OSError, ValueError, TypeError, DeepSeekError):
            # A bad model response must not pin future attempts to bad data.
            pass
    value = factory()
    validate(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return value


def _strict_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{1,5}", value.strip()):
        return int(value.strip())
    return None


def _discover_toc(page_count: int, front: int, back: int, renderer: PageRenderer,
                  client: DeepSeekClient, cache_dir: Path) -> list[int]:
    prompt = (
        "你在查找书籍印刷目录。逐张判断图片是否属于目录/目次/Contents（包括没有重复标题的续页）。"
        "目录页应有章节标题与对应页码；正文、索引、广告和只有标题的封面不算。"
        "只返回 JSON 对象，例如 {\"toc_pages\":[3,4]}；数字是标注的 PDF 页序。"
        "不要推测未给出的页。"
    )

    def inspect(numbers: list[int]) -> list[int]:
        def parse(raw: dict[str, Any]) -> list[int]:
            found = raw.get("toc_pages")
            if not isinstance(found, list):
                raise DeepSeekError("DeepSeek 目录定位结果缺少 toc_pages 列表")
            parsed = [_strict_int(value) for value in found]
            if any(value is None or value not in numbers for value in parsed):
                raise DeepSeekError("DeepSeek 目录定位结果包含未提交的页码")
            return sorted(set(parsed))

        name = "locate-" + "-".join(map(str, numbers))
        raw = _cached(cache_dir, name, lambda: client.ask_json(
            prompt, [(number, renderer.render(number, scale=1150)) for number in numbers],
            max_tokens=500), parse)
        return parse(raw)

    front_pages = list(range(1, min(front, page_count) + 1))
    back_pages = list(range(max(front_pages[-1] + 1 if front_pages else 1,
                                page_count - back + 1), page_count + 1)) if back else []
    found: list[int] = []
    for numbers in (front_pages[i:i + 6] for i in range(0, len(front_pages), 6)):
        found.extend(inspect(numbers))
    if not found:
        for numbers in (back_pages[i:i + 6] for i in range(0, len(back_pages), 6)):
            found.extend(inspect(numbers))
    return sorted(set(found))


def _parse_toc_entries(raw: dict[str, Any], pdf_page: int, page_count: int
                       ) -> tuple[list[TocEntry], list[str]]:
    if (not isinstance(raw.get("is_toc"), bool)
            or not isinstance(raw.get("uncertain"), bool)
            or not isinstance(raw.get("entries"), list)):
        raise DeepSeekError(f"第 {pdf_page} 页目录结果缺少 is_toc、uncertain 或 entries")
    warnings: list[str] = []
    if raw.get("uncertain") is True:
        warnings.append(f"PDF 第 {pdf_page} 页有模型无法辨认的目录内容")
    if not raw["is_toc"]:
        if raw["entries"]:
            warnings.append(f"PDF 第 {pdf_page} 页的目录判断与条目互相矛盾")
        return [], warnings
    entries: list[TocEntry] = []
    for index, item in enumerate(raw["entries"], 1):
        if not isinstance(item, dict):
            raise DeepSeekError(f"第 {pdf_page} 页第 {index} 条目录格式无效")
        title = item.get("title")
        label = item.get("printed_page")
        level = _strict_int(item.get("level"))
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
            raise DeepSeekError(f"第 {pdf_page} 页第 {index} 条目录标题无效")
        clean_title = re.sub(r"\s+", " ", title).strip(" .…·\t")
        if (not isinstance(label, (str, int)) and label is not None) or isinstance(label, bool):
            raise DeepSeekError(
                f"第 {pdf_page} 页第 {index} 条目录页码无效："
                f"{clean_title[:36]!r}，模型返回 {str(label)[:32]!r}")
        value = _page_value(str(label))
        if not value or value[0] != "arabic":
            # Skip printed Roman numerals, missing labels, and illegible labels.
            # A decimal integer that is invalid remains a model error.
            if _arabic_page_digits(str(label)) is not None:
                raise DeepSeekError(
                    f"第 {pdf_page} 页第 {index} 条目录页码无效："
                    f"{clean_title[:36]!r}，模型返回 {str(label)[:32]!r}")
            continue
        if level is None or not 1 <= level <= 6:
            raise DeepSeekError(f"第 {pdf_page} 页第 {index} 条目录层级无效")
        if not 1 <= value[1] <= page_count + 200:
            raise DeepSeekError(
                f"第 {pdf_page} 页第 {index} 条目录页码无效："
                f"{clean_title[:36]!r}，模型返回 {str(label)[:32]!r}")
        entries.append(TocEntry(clean_title, value[1], "arabic", level, pdf_page,
                                f"{title} {label}", 1.0))
    if not entries and not raw["entries"]:
        warnings.append(f"PDF 第 {pdf_page} 页被判为目录，但没有识别出条目")
    return entries, warnings


def _extract_toc(page_number: int, page_count: int, renderer: PageRenderer,
                 client: DeepSeekClient, cache_dir: Path
                 ) -> tuple[list[TocEntry], list[str], list[dict[str, Any]]]:
    prompt = (
        "请从这页书籍的印刷目录准确抄录所有有明确印刷页码的条目。两张图是同一 PDF 页的上下半页，"
        "中间有重叠；重叠条目只输出一次。双栏按左栏从上到下，再右栏从上到下。"
        "标题保留原文及章/节编号，去掉点线和右端页码；不要补造看不清的字或页码。"
        "printed_page 必须是印刷页码原样字符串（阿拉伯数字或罗马数字），level 是 1 到 6 的整数。"
        "level 必须优先按印刷目录中的视觉层级判断：结合左侧缩进、对齐、字号和间距，"
        "同一视觉层级的条目给相同 level，不论标题有无章节编号。"
        "章节编号只是辅助线索，不能仅因标题没有编号就把它升为一级。"
        "例如 Chapter 5. The Eye 是一级；若 Bibliography、Exercises 与 5.1、5.2 同缩进，"
        "它们都属于 Chapter 5 下的二级；只有与章节标题同一视觉层级的全书附录或参考文献才是一级。"
        "无法读清的行请略过并置 uncertain=true。非目录页置 is_toc=false。"
        "只返回 JSON，例如："
        "{\"is_toc\":true,\"uncertain\":false,\"entries\":["
        "{\"title\":\"第一章 绪论\",\"printed_page\":\"1\",\"level\":1},"
        "{\"title\":\"1.1 背景\",\"printed_page\":\"3\",\"level\":2}]}"
    )

    def request() -> dict[str, Any]:
        image = renderer.render(page_number, scale=2200)
        midpoint = image.height // 2
        overlap = max(40, int(image.height * 0.04))
        upper = image.crop((0, 0, image.width, min(image.height, midpoint + overlap)))
        lower = image.crop((0, max(0, midpoint - overlap), image.width, image.height))
        return client.ask_json(prompt, [(page_number, upper), (page_number, lower)], max_tokens=8192)

    raw = _cached(cache_dir, f"toc-{TOC_PROMPT_VERSION}-{page_number}", request,
                  lambda value: _parse_toc_entries(value, page_number, page_count))
    entries, warnings = _parse_toc_entries(raw, page_number, page_count)
    omitted = []
    if raw["is_toc"]:
        for item in raw["entries"]:
            label = item.get("printed_page")
            parsed = _page_value(str(label))
            if not parsed or parsed[0] != "arabic":
                omitted.append({"title": item["title"], "printed_page": label,
                                "source_page": page_number})
    return entries, warnings, omitted


class VisionPageLabels:
    """Printed page labels from native PDF text or DeepSeek image inspection."""

    def __init__(self, pdf_path: Path, page_count: int, renderer: PageRenderer,
                 client: DeepSeekClient, cache_dir: Path) -> None:
        self.pdf_path = pdf_path
        self.page_count = page_count
        self.renderer = renderer
        self.client = client
        self.cache_dir = cache_dir
        self.labels: dict[int, list[tuple[str, int]]] = {}

    def _native(self, pdf: Any, page_number: int) -> list[tuple[str, int]]:
        page = pdf.pages[page_number - 1]
        words = _pdfplumber_words(page)
        content = PageContent(page_number, float(page.width), float(page.height),
                              _group_words(words, float(page.width), float(page.height)), "text")
        return _labels_in_edges(content)

    def _edge_image(self, page_number: int) -> Image.Image:
        image = self.renderer.render(page_number, scale=1750)
        band = max(1, round(image.height * 0.18))
        output = Image.new("RGB", (image.width, band * 2 + 24), "white")
        output.paste(image.crop((0, 0, image.width, band)), (0, 0))
        output.paste(image.crop((0, image.height - band, image.width, image.height)),
                     (0, band + 24))
        return output

    def _parse_labels(self, raw: dict[str, Any], batch: list[int]
                      ) -> dict[int, list[tuple[str, int]]]:
        labels = raw.get("labels")
        if not isinstance(labels, list):
            raise DeepSeekError("DeepSeek 页码结果缺少 labels 列表")
        parsed: dict[int, list[tuple[str, int]]] = {}
        for item in labels:
            if not isinstance(item, dict):
                raise DeepSeekError("DeepSeek 页码结果格式无效")
            number = _strict_int(item.get("pdf_page"))
            printed = item.get("printed_page")
            if (number not in batch or not isinstance(printed, (str, int))
                    or isinstance(printed, bool)):
                raise DeepSeekError("DeepSeek 页码结果包含无效 PDF 页序")
            page_label = _page_value(str(printed))
            if not page_label or page_label[1] > self.page_count + 200:
                raise DeepSeekError("DeepSeek 页码结果包含无效印刷页码")
            if number in parsed:
                raise DeepSeekError("DeepSeek 为同一 PDF 页返回多个页码")
            parsed[number] = [page_label]
        return parsed

    def _reuse_cached_labels(self, numbers: list[int]) -> list[int]:
        """Read validated prior batches even when today's batch grouping differs."""
        prefix = f"{PROMPT_VERSION}-{DEEPSEEK_MODEL}-labels-"
        needed = set(numbers)
        reused: dict[int, list[tuple[str, int]]] = {}
        conflicts: set[int] = set()
        if not self.cache_dir.is_dir():
            return numbers
        for path in self.cache_dir.glob(prefix + "*.json"):
            suffix = path.stem[len(prefix):]
            if not re.fullmatch(r"\d+(?:-\d+)*", suffix):
                continue
            batch = [int(value) for value in suffix.split("-")]
            relevant = needed.intersection(batch)
            if not relevant:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                parsed = self._parse_labels(raw, batch)
            except (OSError, ValueError, TypeError, DeepSeekError):
                continue
            for number in relevant:
                value = parsed.get(number, [])
                if number in reused and reused[number] != value:
                    conflicts.add(number)
                else:
                    reused[number] = value
        for number in needed - conflicts:
            if number in reused:
                self.labels[number] = reused[number]
        return [number for number in numbers if number not in self.labels]

    def prefetch(self, pdf: Any, numbers: list[int]) -> None:
        missing: list[int] = []
        for number in numbers:
            if number in self.labels:
                continue
            try:
                native = self._native(pdf, number) if pdf is not None else []
            except (PdfminerException, PDFException):
                # A damaged native text layer must not discard usable page
                # images or already recognized table-of-contents entries.
                native = []
            if len(native) == 1:
                self.labels[number] = native
            else:
                missing.append(number)
        missing = self._reuse_cached_labels(missing)
        prompt = (
            "这些图片各为一页的页眉与页脚拼图，中间白色横带分隔。"
            "只读书籍的印刷页码（通常是边缘单独的阿拉伯数字或罗马数字），"
            "不要把章号、节号、年份、公式号或正文数字当页码。"
            "没有可辨页码则省略该页；每页最多一个。"
            "返回 JSON：{\"labels\":[{\"pdf_page\":12,\"printed_page\":\"7\"}]}。"
        )
        for start in range(0, len(missing), 4):
            batch = missing[start:start + 4]
            name = "labels-" + "-".join(map(str, batch))
            raw = _cached(self.cache_dir, name, lambda: self.client.ask_json(
                prompt, [(number, self._edge_image(number)) for number in batch],
                max_tokens=800), lambda value: self._parse_labels(value, batch))
            self.labels.update({number: [] for number in batch})
            self.labels.update(self._parse_labels(raw, batch))

    def footer_labels(self, pdf: Any, page_number: int) -> list[tuple[str, int]]:
        if page_number not in self.labels:
            self.prefetch(pdf, [page_number])
        return self.labels[page_number]


def process_book_deepseek(source: Path, output: Path, cache_dir: Path, *, api_key: str,
                          front: int = 35, back: int = 12, dry_run: bool = False,
                          skip_existing: bool = True,
                          verify_existing: bool = False,
                          skip_bookmarked: bool = False,
                          client: DeepSeekClient | None = None,
                          renderer: PageRenderer | None = None) -> BookResult:
    result = BookResult(str(source), "failed")
    usage_before = dict(client.api_usage) if client is not None and isinstance(
        getattr(client, "api_usage", None), dict) else {}
    try:
        reader = PdfReader(source, strict=False)
        if reader.is_encrypted:
            result.status = "needs_review"
            result.error = "PDF 已加密，需要密码"
            return result
        result.page_count = len(reader.pages)
        if not result.page_count:
            result.error = "PDF 没有页面"
            return result
        result.existing_bookmarks = _outline_count(reader.outline)
        if skip_bookmarked and result.existing_bookmarks:
            result.status = "skipped"
            result.warnings.append(f"已有 {result.existing_bookmarks} 条书签，按选项快速跳过")
            return result
        result.existing_outline_quality = _outline_quality(reader.outline)
        outline_action = _outline_action(result.existing_bookmarks, result.page_count,
                                         result.existing_outline_quality,
                                         skip_existing=skip_existing)
        if outline_action == "skip" and not verify_existing:
            result.status = "skipped"
            result.warnings.append(f"已有 {result.existing_bookmarks} 条可用章节书签")
            return result
        client = client or DeepSeekClient(api_key)
        renderer = renderer or PageRenderer(source)
        cache_dir.mkdir(parents=True, exist_ok=True)
        pages = _discover_toc(result.page_count, front, back, renderer, client, cache_dir)
        result.toc_pages = pages
        if not pages:
            result.status = "needs_review"
            result.warnings.append("没有找到可信的印刷目录页")
            return result
        if pages[-1] - pages[0] > 16:
            result.status = "needs_review"
            result.warnings.append("候选目录页分布超过 16 页，无法确定连续目录范围")
            return result
        # A classification pass can miss continuation pages without a heading.
        # Inspect gaps and immediate neighbors with the larger, split images.
        candidate_pages = list(range(pages[0], pages[-1] + 1))
        extracted: dict[int, list[TocEntry]] = {}
        toc_rows: set[int] = set()

        def inspect_toc(number: int) -> bool:
            page_entries, warnings, omitted = _extract_toc(
                number, result.page_count, renderer, client, cache_dir)
            extracted[number] = page_entries
            result.warnings.extend(warnings)
            if omitted:
                result.toc_corrections.append({"action": "omit_non_numeric_page",
                                               "entries": omitted})
            if page_entries or omitted:
                toc_rows.add(number)
            elif number in candidate_pages:
                result.warnings.append(f"PDF 第 {number} 页位于目录范围内却没有目录条目")
            return bool(page_entries or omitted)

        for number in candidate_pages:
            inspect_toc(number)
        for direction, start in ((-1, candidate_pages[0] - 1),
                                 (1, candidate_pages[-1] + 1)):
            number = start
            misses = 0
            while 1 <= number <= result.page_count and len(extracted) < 20 and misses < 2:
                misses = 0 if inspect_toc(number) else misses + 1
                number += direction
        pages = sorted(toc_rows)
        result.toc_pages = pages
        if any(right - left > 1 for left, right in zip(pages, pages[1:])):
            result.warnings.append("目录页不连续，需要人工确认是否漏页")
        entries = [entry for number in pages for entry in extracted[number]]
        entries = normalize_levels(unique_entries(entries))
        # Keep the recognized directory in the report if later page-label
        # analysis cannot establish safe bookmark destinations.
        result.entries = [entry.as_dict() for entry in entries]
        if len(entries) < 4:
            result.warnings.append("目录条目少于 4 条，需要人工确认")
        arabic = [entry.printed_page for entry in entries if entry.numbering == "arabic"]
        regressions = sum(later < earlier for earlier, later in zip(arabic, arabic[1:]))
        if regressions:
            result.warnings.append(f"目录页码出现 {regressions} 次倒退")
        if not entries:
            result.status = "needs_review"
            return result
        try:
            native_pdf = pdfplumber.open(source)
        except (PdfminerException, PDFException):
            # PDFium and pypdf can read some PDFs whose pdfminer document
            # parser rejects /Root. Use the rendered page edges for labels.
            native_pdf = None
        with native_pdf if native_pdf is not None else nullcontext(None) as pdf:
            labels = VisionPageLabels(source, result.page_count, renderer, client, cache_dir)
            anchors = (_verified_outline_page_anchors(reader, pdf, labels, pages)
                       if {entry.numbering for entry in entries} == {"arabic"} else [])
            if not anchors:
                labels.prefetch(pdf, _sample_body_pages(pages, result.page_count))
                anchors = _collect_anchors(pdf, labels, pages, entries)
            result.offsets, result.offset_segments, warnings = _fit_offsets(
                anchors, pdf, labels, {entry.numbering for entry in entries})
        result.anchors = [asdict(anchor) for anchor in anchors]
        result.warnings.extend(warnings)
        result.warnings.extend(_map_entries(entries, result.offsets, result.offset_segments,
                                            result.page_count))
        result.entries = [entry.as_dict() for entry in entries]
        if len(entries) < 4 or any(entry.pdf_page is None for entry in entries) or result.warnings:
            result.status = "needs_review"
            return result
        if verify_existing and result.existing_bookmarks:
            result.existing_outline_check = _compare_existing_outline(reader, entries, pages)
            if result.existing_outline_check["matches"] and skip_existing:
                result.status = "skipped"
                return result
            concern = _in_place_replacement_review_reason(
                source, output, result.existing_outline_check,
                result.existing_outline_quality, skip_existing=skip_existing)
            if concern:
                result.status = "needs_review"
                result.warnings.append(concern)
                return result
            outline_action = "replace"
        result.status = "dry_run" if dry_run else "success"
        # The contents page gets its own bookmark, ahead of every entry.
        contents = toc_page_bookmark(entries, pages[0])
        result.toc_bookmark = contents.as_dict()
        if not dry_run:
            _write_pdf(source, output, [contents, *entries],
                       preserve_existing=outline_action == "preserve")
            result.output = str(output)
        return result
    except Exception as error:
        result.status = "failed"
        result.error = f"{type(error).__name__}: {error}"
        return result
    finally:
        if client is not None and isinstance(getattr(client, "api_usage", None), dict):
            result.api_usage = {
                key: max(0, value - usage_before.get(key, 0))
                for key, value in client.api_usage.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
