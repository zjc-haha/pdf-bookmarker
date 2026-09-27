"""Fast, local PDF metadata and cover previews for the desktop interface.

No page text is extracted and no network request is made here. The DeepSeek
processing flow determines the final result after the user starts a run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PIL import Image
from pypdf import PdfReader

from .__main__ import _pdf_files
from .deepseek import PageRenderer
from .pipeline import (_outline_action, _outline_count, _outline_page_label_ratio,
                       _outline_quality)


class PreviewError(RuntimeError):
    """A PDF cover could not be previewed."""


@dataclass(frozen=True)
class BookmarkNode:
    title: str
    page_number: int | None  # 1-based PDF page; None if the destination is invalid.
    children: tuple["BookmarkNode", ...] = ()


@dataclass(frozen=True)
class PdfPreview:
    path: Path
    page_count: int
    bookmark_count: int
    outline_quality: float
    eligible: bool
    reason: str
    bookmarks: tuple[BookmarkNode, ...] = ()


def list_pdf_paths(folder: Path, output_dir: Path) -> list[Path]:
    """Return the same recursive, output-excluding PDF paths as CLI batch mode.

    This is only the inexpensive filesystem pass.  Call :func:`inspect_pdf`
    for each path in a worker thread to decide whether it will be processed.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(str(folder))
    return _pdf_files(folder, Path(output_dir))


def _outline_tree(reader: PdfReader, items: list[Any]) -> tuple[BookmarkNode, ...]:
    """Convert pypdf's item, child-list structure into nested nodes."""
    nodes: list[BookmarkNode] = []
    for item in items:
        if isinstance(item, list):
            children = _outline_tree(reader, item)
            if nodes:
                parent = nodes[-1]
                nodes[-1] = replace(parent, children=parent.children + children)
            else:
                # A malformed outline can begin with a child list.  Still show
                # its contents instead of losing them from the preview.
                nodes.extend(children)
            continue
        try:
            title = str(item.get("/Title", ""))
        except (AttributeError, TypeError, ValueError):
            title = str(item)
        try:
            index = reader.get_destination_page_number(item)
            page_number = index + 1 if isinstance(index, int) and index >= 0 else None
        except Exception:
            # A broken destination need not hide other, usable bookmarks.
            page_number = None
        nodes.append(BookmarkNode(title=title, page_number=page_number))
    return tuple(nodes)


def inspect_pdf(path: Path, *, replace_existing: bool = False,
                verify_existing: bool = False,
                skip_bookmarked: bool = False,
                include_bookmarks: bool = True) -> PdfPreview:
    """Read page count and existing outline without opening page contents.

    ``eligible`` uses the same existing-outline decision as the DeepSeek
    processing flow. Set ``include_bookmarks=False`` for a folder's first metadata
    pass; inspect the selected book again to obtain its complete bookmark tree.
    Corrupt, empty and encrypted PDFs get a reason for the GUI and are never
    offered for processing in its candidate list.
    """
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        return PdfPreview(path, 0, 0, 0.0, False, "不是 PDF 文件")
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            return PdfPreview(path, 0, 0, 0.0, False, "PDF 已加密，需要密码")
        page_count = len(reader.pages)
        if not page_count:
            return PdfPreview(path, 0, 0, 0.0, False, "PDF 没有页面")
        outline = reader.outline
        count = _outline_count(outline)
        if skip_bookmarked and count:
            bookmarks = _outline_tree(reader, outline) if include_bookmarks else ()
            return PdfPreview(path, page_count, count, 0.0, False,
                              f"已有 {count} 条书签，按选项快速跳过", bookmarks)
        quality = _outline_quality(outline)
        action = _outline_action(count, page_count, quality,
                                 skip_existing=not replace_existing)
        bookmarks = _outline_tree(reader, outline) if include_bookmarks else ()
        if action == "skip" and not verify_existing:
            return PdfPreview(path, page_count, count, quality, False,
                              f"已有 {count} 条可用章节书签，默认跳过", bookmarks)
        if not count:
            reason = "尚无书签；目录识别可信后将添加"
        elif replace_existing:
            reason = f"已选择强制替换；目录识别可信后将替换现有 {count} 条书签"
        elif verify_existing and action in {"skip", "preserve"}:
            reason = (f"现有 {count} 条书签待与印刷目录核对；"
                      "一致则跳过，缺失或不一致则替换")
        elif action == "preserve":
            reason = f"现有 {count} 条可用书签；目录识别可信后将保留并补充"
        else:
            issue = ("多为页码占位项" if _outline_page_label_ratio(outline) >= 0.5
                     else "标题质量不足")
            reason = f"现有 {count} 条书签{issue}；目录识别可信后将替换"
        return PdfPreview(path, page_count, count, quality, True, reason, bookmarks)
    except Exception as error:
        detail = str(error).strip()
        message = f"无法读取 PDF：{type(error).__name__}"
        if detail:
            message += f"：{detail[:160]}"
        return PdfPreview(path, 0, 0, 0.0, False, message)


def render_page(path: Path, page_number: int, *, max_size: int = 900) -> Image.Image:
    """Render one 1-based PDF page using the bundled PDFium library.

    The returned RGB Pillow image owns its pixels and remains valid after the
    renderer closes its native bitmap. Call this from a background thread.
    """
    if isinstance(max_size, bool) or not isinstance(max_size, int) or not 100 <= max_size <= 2400:
        raise ValueError("max_size 必须在 100 到 2400 之间")
    if isinstance(page_number, bool) or not isinstance(page_number, int):
        raise ValueError("页码必须是整数")
    path = Path(path)
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            raise PreviewError("PDF 已加密，需要密码")
        page_count = len(reader.pages)
        if not page_count:
            raise PreviewError("PDF 没有页面")
    except PreviewError:
        raise
    except Exception as error:
        raise PreviewError(
            f"无法预览 PDF 第 {page_number} 页：{type(error).__name__}: {error}"
        ) from error
    if not 1 <= page_number <= page_count:
        raise ValueError(f"页码超出范围：应在 1 到 {page_count} 之间")
    try:
        return PageRenderer(path).render(page_number, scale=max_size)
    except Exception as error:
        raise PreviewError(
            f"无法预览 PDF 第 {page_number} 页：{type(error).__name__}: {error}"
        ) from error


def render_first_page(path: Path, *, max_size: int = 900) -> Image.Image:
    """Backward-compatible shortcut for rendering the PDF cover page."""
    return render_page(path, 1, max_size=max_size)
