"""Desktop workspace for previewing PDFs and adding bookmarks."""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import uuid
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from PIL import Image, ImageTk

from .__main__ import _done_files
from .gui_report import RunReport
from .key_cache import KeyCacheError, load_key, save_key
from .pipeline import _outline_action, recover_pending_overwrites
from .toc import HIERARCHY_VERSION
from .preview import (BookmarkNode, PdfPreview, inspect_pdf, list_pdf_paths,
                      reclassify_preview, render_first_page, render_page)
from .storage import (LegacyDataMigrationError, job_data_dir,
                      migrate_legacy_job_data)
from .ui_style import COLORS, RoundedButton, WrapLabel, apply_theme, font, icon


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKER_NAME = "PDF书签命令行.exe"
REPORT_NAME = "bookmarker-summary.csv"

BG = COLORS.background
WHITE = COLORS.surface
TEXT = COLORS.text
MUTED = COLORS.muted
BORDER = COLORS.border
BLUE = COLORS.blue
PALE_BLUE = COLORS.blue_pale
FAILURE = COLORS.failure
FAILURE_PALE = COLORS.failure_pale


def default_directories(*, frozen: bool | None = None) -> tuple[Path, Path, Path]:
    """Return input, output, and working directories for this installation."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        for location in (PROJECT_ROOT.parent, PROJECT_ROOT):
            source = location / "books"
            if source.is_dir():
                return source, PROJECT_ROOT / "output", PROJECT_ROOT
        return PROJECT_ROOT, PROJECT_ROOT / "output", PROJECT_ROOT

    install_dir = Path(sys.executable).resolve().parent
    return install_dir, install_dir / "output", install_dir


def worker_executable(*, frozen: bool | None = None) -> Path | None:
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return None
    worker = Path(sys.executable).resolve().with_name(WORKER_NAME)
    if not worker.is_file():
        raise FileNotFoundError(f"程序目录中缺少 {WORKER_NAME}：{worker}")
    return worker


def build_batch_command(
    input_path: Path,
    output_dir: Path,
    *,
    single_file: bool = False,
    dry_run: bool = False,
    replace_existing: bool = False,
    verify_existing: bool = False,
    skip_bookmarked: bool = False,
    overwrite_original: bool = False,
    resume: bool = True,
    stop_file: Path | None = None,
    frozen: bool | None = None,
) -> list[str]:
    """Build an argument list so paths with spaces or Chinese text stay intact."""
    worker = worker_executable(frozen=frozen)
    launcher = [str(worker)] if worker else [sys.executable, "-m", "bookmarker"]
    command = launcher + [
        "single" if single_file else "batch",
        str(input_path),
        "--output",
        str(output_dir),
    ]
    if dry_run:
        command.append("--dry-run")
    if replace_existing:
        command.append("--replace-existing")
    if verify_existing:
        command.append("--verify-existing")
    if skip_bookmarked:
        command.append("--skip-bookmarked")
    if overwrite_original:
        command.append("--overwrite-original")
    if resume:
        command.append("--resume")
    if stop_file is not None:
        command.extend(("--stop-file", str(stop_file)))
    return command


def build_batch_environment(*, api_key: str = "") -> dict[str, str]:
    """Pass the DeepSeek key to the worker without exposing it in argv."""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUNBUFFERED"] = "1"
    if api_key:
        environment["DEEPSEEK_API_KEY"] = api_key
    if getattr(sys, "frozen", False):
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def _file_size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "大小未知"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _directory_pages_text(pages: object) -> str:
    if not isinstance(pages, (list, tuple)):
        return ""
    numbers = [page for page in pages if isinstance(page, int) and page > 0]
    if not numbers:
        return ""
    if len(numbers) > 1 and numbers == list(range(numbers[0], numbers[-1] + 1)):
        return f"PDF 第 {numbers[0]}—{numbers[-1]} 页"
    shown = "、".join(str(page) for page in numbers[:6])
    suffix = f"等 {len(numbers)} 页" if len(numbers) > 6 else " 页"
    return f"PDF 第 {shown}{suffix}"


def _friendly_issue(value: object) -> str:
    detail = re.sub(r"^[A-Za-z][A-Za-z0-9_]*(?:Error|Exception):\s*", "",
                    str(value or "").strip())
    if not detail:
        return ""
    lower = detail.casefold()
    if "401" in lower or "unauthorized" in lower:
        return "DeepSeek API Key 无效或已失效"
    if "429" in lower or "rate limit" in lower:
        return "请求过于频繁或额度受限，请稍后重试"
    if "timeout" in lower or "timed out" in lower:
        return "网络请求超时，请检查网络后重试"
    if "is_toc" in lower or "uncertain" in lower:
        return "目录识别结果格式异常，请重试"
    if (re.search(r"[\u4e00-\u9fff]", detail)
            and not re.search(r"traceback|exception|http error|[A-Za-z]:\\", detail, re.I)):
        return detail[:140] + ("…" if len(detail) > 140 else "")
    return "详细原因请查看报告"


def _token_usage_text(rows: dict[str, dict], *, completed: bool,
                      all_skipped: bool = False) -> str:
    """Summarize only usage reported by the current processing run."""
    def amount(value: object) -> int:
        try:
            return max(0, int(value)) if value is not None and not isinstance(value, bool) else 0
        except (TypeError, ValueError):
            return 0

    if not rows:
        if completed and all_skipped:
            return "本次新增 Token：0（全部文件沿用此前结果）"
        return "" if not completed else "本次未收到 API Token 用量记录"
    prompt = completion = total = reported = unreported = missing = 0
    for row in rows.values():
        usage = row.get("api_usage")
        if not isinstance(usage, dict):
            missing += 1
            continue
        prompt += amount(usage.get("prompt_tokens"))
        completion += amount(usage.get("completion_tokens"))
        total += amount(usage.get("total_tokens"))
        reported += amount(usage.get("reported_responses"))
        unreported += amount(usage.get("unreported_responses"))
    if reported or total:
        message = (f"本次已统计 API Token：输入 {prompt:,}，输出 {completion:,}，"
                   f"合计 {total:,}")
    elif not missing and not unreported and all(
        row.get("status") in {"skipped", "dry_run", "success", "needs_review"}
        for row in rows.values()
    ):
        message = "本次已统计 Token：0（使用已有识别缓存或跳过）"
    else:
        message = "本次未获取到可统计的 API Token 用量"
    if unreported:
        message += f"；另有 {unreported} 次响应未提供用量"
    if missing:
        message += f"；{missing} 本缺少用量记录"
    if any(row.get("status") == "failed" for row in rows.values()):
        message += "；失败请求的用量可能未计入"
    return message


def _processing_label(preview: PdfPreview, *, replace_existing: bool = False,
                      verify_existing: bool = False, dry_run: bool = False) -> str:
    """The expected outline action; recognition can still change the outcome."""
    if not preview.eligible:
        return "无法读取" if preview.page_count <= 0 else "跳过"
    if not preview.bookmark_count:
        return "新增"
    if verify_existing and not replace_existing:
        return "核对"
    action = _outline_action(preview.bookmark_count, preview.page_count,
                             preview.outline_quality,
                             skip_existing=not replace_existing)
    return {"skip": "跳过", "preserve": "补充", "replace": "替换"}[action]


def _display_reason(preview: PdfPreview, *, dry_run: bool = False) -> str:
    if dry_run and preview.eligible:
        return f"仅分析，不生成 PDF。正式运行时：{preview.reason}"
    return preview.reason


def _file_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _resume_skips(
    path: Path,
    source_dir: Path,
    output_dir: Path,
    old: dict | None,
    *,
    dry_run: bool,
    replace_existing: bool,
    verify_existing: bool = False,
    skip_bookmarked: bool = False,
    overwrite_original: bool = False,
) -> bool:
    """Mirror the CLI's resume decision for one candidate PDF."""
    if not old:
        return False
    try:
        stat = path.stat()
        relative = path.relative_to(source_dir)
    except (OSError, ValueError):
        return False
    from .deepseek import DEEPSEEK_MODEL, PROMPT_VERSION
    options = {
        "engine": "deepseek", "model": DEEPSEEK_MODEL, "prompt_version": PROMPT_VERSION,
        "hierarchy_version": HIERARCHY_VERSION,
        "front": 35, "back": 12,
        "replace_existing": replace_existing,
        "verify_existing": verify_existing,
        "skip_bookmarked": skip_bookmarked, "dry_run": dry_run,
        "overwrite_original": overwrite_original,
    }
    old_options = old.get("options") if isinstance(old.get("options"), dict) else {}
    old_options = {"verify_existing": False, "skip_bookmarked": False,
                   "overwrite_original": False, **old_options}
    old_options = {key: value for key, value in old_options.items() if key in options}
    if (old.get("source_size") != stat.st_size
            or old.get("source_mtime_ns") != stat.st_mtime_ns
            or old_options != options):
        return False
    finished = old.get("status") in (
        {"skipped", "dry_run"} if dry_run else {"success", "skipped"})
    output = (path if overwrite_original else output_dir / relative.parent /
              (path.stem + "_deepseek_bookmarked.pdf"))
    return finished and (dry_run or old.get("status") == "skipped" or output.is_file())


class BookmarkApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("PDF 书签工作台")
        usable_width = max(1000, root.winfo_screenwidth() - 64)
        usable_height = max(620, root.winfo_screenheight() - 80)
        min_width = min(1120, usable_width)
        min_height = min(700, usable_height)
        self.root.geometry(f"{min(1500, usable_width)}x{min(850, usable_height)}")
        self.root.minsize(min_width, min_height)
        self.root.configure(bg=BG)
        self._configure_style()
        self._icons: list[ImageTk.PhotoImage] = []

        default_input, default_output, self.working_dir = default_directories()
        self.input_var = tk.StringVar(
            value=str(default_input) if default_input != self.working_dir else "")
        self._suggested_output = str(default_output)
        self.output_var = tk.StringVar(value=self._suggested_output)
        key_cache_error = ""
        try:
            cached_api_key = load_key()
        except (KeyCacheError, OSError) as error:
            cached_api_key = ""
            key_cache_error = str(error)
        self.api_key_var = tk.StringVar(value=cached_api_key)
        self._cached_api_key = cached_api_key
        self.dry_run_var = tk.BooleanVar(value=False)
        self.replace_var = tk.BooleanVar(value=False)
        self.verify_var = tk.BooleanVar(value=True)
        self.skip_bookmarked_var = tk.BooleanVar(value=False)
        self.policy_var = tk.StringVar(value="verify")
        self.compare_on_replace_var = tk.BooleanVar(value=False)
        self.overwrite_original_var = tk.BooleanVar(value=False)
        self.resume_var = tk.BooleanVar(value=False)
        self.search_var = tk.StringVar()
        self.filter_var = tk.StringVar(value="全部")
        self.status_var = tk.StringVar(value="就绪")
        self.report_var = tk.StringVar(value="处理后可查看报告")
        self.run_summary_var = tk.StringVar(value="选择 PDF 或文件夹后显示本次范围；预览不会调用 DeepSeek")
        self.token_usage_var = tk.StringVar(value="")
        self.settings_summary_var = tk.StringVar(value="核对并修正 · 保存副本 · 生成 PDF")
        self.count_var = tk.StringVar(value="尚未选择 PDF")
        self.source_mode_var = tk.StringVar(value="选择 PDF 或文件夹")
        self.list_hint_var = tk.StringVar(value="选择单个 PDF 或文件夹")
        self.preview_title_var = tk.StringVar(value="选择左侧文件")
        self.preview_meta_var = tk.StringVar(value="首页将在这里显示")
        self.page_var = tk.StringVar(value="1")
        self.page_total_var = tk.StringVar(value="/ 0")
        self.zoom_var = tk.StringVar(value="100%")
        self.outline_count_var = tk.StringVar(value="现有书签")
        self.outline_action_var = tk.StringVar(value="选择文件后显示处理方式")
        self.outline_hint_var = tk.StringVar(value="选择文件后显示原有书签")
        self.result_detail_var = tk.StringVar(value="处理后可在此查看识别书签和复核原因")
        self.result_count_var = tk.StringVar(value="尚无本次结果")

        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._process: subprocess.Popen[str] | None = None
        self._report_path: Path | None = None
        self._report_before: tuple[int, int] | None = None
        self._run_report = None
        self._run_results: dict[str, dict] = {}
        self._run_status: dict[str, str] = {}
        self._stop_requested = False
        self._stop_file: Path | None = None
        self._close_when_done = False
        self._running = False
        self._scanning = False
        self._closed = False
        self._source_path: Path | None = None
        self._source_is_file = False
        self._displayed_path: Path | None = None
        self._loaded_signature: tuple[object, ...] | None = None
        self._scan_generation = 0
        self._scan_cancel = threading.Event()
        self._selection_generation = 0
        self._render_generation = 0
        self._previews: dict[str, PdfPreview] = {}
        self._base_previews: dict[str, PdfPreview] = {}
        self._file_indices: dict[str, int] = {}
        self._scan_fingerprints: dict[str, tuple[int, int] | None] = {}
        self._resume_done: dict[str, dict] = {}
        self._resume_done_loaded = False
        self._resume_load_pending = False
        self._resume_load_error = ""
        self._post_run_message: tuple[str, str] | None = None
        self._source_changed_before_run = False
        self._worker_total = 0
        self._worker_seen_paths: set[str] = set()
        self._logged_results: dict[str, str] = {}
        self._progress_heading_ranges: dict[str, list[tuple[str, str]]] = {}
        self._scan_total = 0
        self._cover_cache: OrderedDict[tuple[Path, int], Image.Image] = OrderedDict()
        self._cover_image: Image.Image | None = None
        self._cover_photo: ImageTk.PhotoImage | None = None
        self._cover_message = "选择左侧文件查看首页"
        self._page_number = 1
        self._page_count = 0
        self._zoom_factor = 1.0
        self._fullscreen_window: tk.Toplevel | None = None
        self._fullscreen_canvas: tk.Canvas | None = None
        self._fullscreen_photo: ImageTk.PhotoImage | None = None
        self._cover_label_wrap_width = 0
        self._outline_label_wrap_width = 0
        self._resize_after: str | None = None
        self._poll_after: str | None = None
        self._load_after: str | None = None
        self._log_visible = False
        self._settings_expanded = False
        self._scan_skipped = 0
        self._scan_failed = 0
        self._scan_resumed = 0
        self._updating_policy = False

        self._build_widgets()
        self._update_output_controls()
        if key_cache_error:
            self._append(f"无法读取已缓存的 API Key：{key_cache_error}")
        self._update_api_key_controls()
        self.replace_var.trace_add("write", self._on_replace_changed)
        self.verify_var.trace_add("write", self._on_replace_changed)
        self.skip_bookmarked_var.trace_add("write", self._on_replace_changed)
        self.overwrite_original_var.trace_add("write", self._on_overwrite_changed)
        self.resume_var.trace_add("write", self._on_replace_changed)
        self.dry_run_var.trace_add("write", self._on_replace_changed)
        self.search_var.trace_add("write", lambda *_: self._rebuild_list())
        self.search_var.trace_add("write", lambda *_: self._update_search_placeholder())
        self.filter_var.trace_add("write", lambda *_: self._rebuild_list())
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._poll_after = self.root.after(100, self._poll_events)
        self._load_after = self.root.after(40, self._load_source)

    def _configure_style(self) -> None:
        self._style = apply_theme(self.root)

    def _icon(self, name: str, *, size: int = 18, color: str = BLUE) -> ImageTk.PhotoImage:
        picture = icon(name, size=size, color=color, master=self.root)
        self._icons.append(picture)
        return picture

    def _badge(self, parent: tk.Misc, name: str, *, size: int = 18,
               background: str = PALE_BLUE, color: str = BLUE) -> tk.Label:
        return tk.Label(parent, image=self._icon(name, size=size, color=color), bg=background,
                        padx=7, pady=7)

    def _card(self, parent: tk.Misc) -> tk.Frame:
        return tk.Frame(parent, bg=WHITE, highlightbackground=BORDER, highlightthickness=1)

    def _label(self, parent: tk.Misc, text: str = "", *, color: str = TEXT,
               size: int = 10, bold: bool = False, **kwargs: object) -> tk.Label:
        return tk.Label(parent, text=text, bg=WHITE, fg=color,
                        font=font(size, bold=bold), **kwargs)

    def _path_field(self, parent: tk.Misc, variable: tk.StringVar,
                    clear: Callable[[], None]) -> tuple[tk.Frame, ttk.Entry, ttk.Button]:
        """Return a bordered path field with an inline clear button."""
        field = tk.Frame(parent, bg=WHITE, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=BLUE)
        field.grid_columnconfigure(0, weight=1)
        entry = ttk.Entry(field, textvariable=variable, style="Field.TEntry")
        entry.grid(row=0, column=0, sticky="ew")
        button = ttk.Button(field, image=self._icon("close", size=14, color=MUTED),
                            style="Icon.TButton", command=clear)
        button.grid(row=0, column=1, padx=(0, 3))
        entry.bind("<FocusIn>", lambda _event: field.configure(highlightbackground=BLUE), add="+")
        entry.bind("<FocusOut>", lambda _event: field.configure(highlightbackground=BORDER),
                   add="+")
        return field, entry, button

    def _build_widgets(self) -> None:
        outer = tk.Frame(self.root, bg=BG, padx=18, pady=10)
        outer.pack(fill="both", expand=True)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(2, weight=1)

        header = tk.Frame(outer, bg=BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._badge(header, "bookmark", size=20, background=BLUE, color=WHITE).pack(
            side="left", padx=(2, 12))
        tk.Label(header, text="PDF 书签工作台", bg=BG, fg=TEXT,
                 font=font(17, bold=True)).pack(side="left")
        tk.Frame(header, bg=BORDER, width=1, height=18).pack(side="left", padx=14)
        tk.Label(header, text="选书 · 预览 · 添加书签", bg=BG, fg=MUTED,
                 font=font(10)).pack(side="left", pady=(2, 0))
        tk.Label(header, text="DEEPSEEK", bg=PALE_BLUE, fg=BLUE,
                 font=font(8, bold=True), padx=10, pady=5).pack(side="right")
        self.settings_button = RoundedButton(header, text="API Key", icon_name="key",
                                              kind="quiet", width=108, height=32,
                                              command=self._focus_api_key)
        self.settings_button.pack(side="right", padx=(0, 10))

        source = self._card(outer)
        source.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        source.grid_columnconfigure(1, weight=1)
        label = tk.Frame(source, bg=WHITE)
        label.grid(row=0, column=0, sticky="w", padx=(16, 14), pady=(14, 6))
        self._badge(label, "pdf", size=16).pack(side="left", padx=(0, 10))
        self._label(label, "来源", bold=True).pack(side="left")
        field, self.input_entry, self.clear_input_button = self._path_field(
            source, self.input_var, self._clear_input)
        field.grid(row=0, column=1, sticky="ew", pady=(14, 6))
        self.input_entry.bind("<Return>", self._on_path_committed, add="+")
        self.input_entry.bind("<FocusOut>", self._on_path_committed, add="+")
        picks = tk.Frame(source, bg=WHITE)
        picks.grid(row=0, column=2, sticky="e", padx=(10, 16), pady=(14, 6))
        self.file_button = RoundedButton(picks, text="选择 PDF", icon_name="pdf",
                                          kind="secondary", width=118, height=36,
                                          command=self._pick_file)
        self.file_button.pack(side="left")
        self.input_button = RoundedButton(picks, text="选择文件夹", icon_name="folder",
                                           kind="secondary", width=128, height=36,
                                           command=self._pick_input)
        self.input_button.pack(side="left", padx=(8, 0))

        label = tk.Frame(source, bg=WHITE)
        label.grid(row=1, column=0, sticky="w", padx=(16, 14), pady=(0, 12))
        self._badge(label, "folder", size=16).pack(side="left", padx=(0, 10))
        self.output_label = self._label(label, "输出", bold=True)
        self.output_label.pack(side="left")
        field, self.output_entry, self.clear_output_button = self._path_field(
            source, self.output_var, self._clear_output)
        field.grid(row=1, column=1, sticky="ew", pady=(0, 12))
        self.output_entry.bind("<Return>", self._on_path_committed, add="+")
        self.output_entry.bind("<FocusOut>", self._on_path_committed, add="+")
        self.output_button = RoundedButton(source, text="浏览输出位置", icon_name="folder-open",
                                            kind="secondary", width=254, height=36,
                                            command=self._pick_output)
        self.output_button.grid(row=1, column=2, sticky="e", padx=(10, 16), pady=(0, 12))

        # The run plan and the processing settings that shape it share one bar.
        self.plan_bar = tk.Frame(source, bg=PALE_BLUE)
        self.plan_bar.grid(row=2, column=0, columnspan=3, sticky="ew", padx=16, pady=(0, 14))
        self.plan_bar.grid_columnconfigure(1, weight=1)
        self.plan_icon = tk.Label(self.plan_bar, image=self._icon("info", size=16),
                                  bg=PALE_BLUE)
        self.plan_icon.grid(row=0, column=0, padx=(12, 6))
        self.run_summary_label = WrapLabel(
            self.plan_bar, textvariable=self.run_summary_var, bg=PALE_BLUE, fg=BLUE,
            font=font(10), anchor="w", justify="left", wraplength=600, pady=9)
        self.run_summary_label.grid(row=0, column=1, sticky="ew")
        self.settings_summary_label = tk.Label(
            self.plan_bar, textvariable=self.settings_summary_var, bg=PALE_BLUE,
            fg=MUTED, font=font(9))
        self.settings_summary_label.grid(row=0, column=2, padx=(12, 10))
        self.settings_toggle = RoundedButton(self.plan_bar, text="处理设置",
                                              icon_name="settings", kind="quiet",
                                              width=112, height=30,
                                              command=self._toggle_settings)
        self.settings_toggle.grid(row=0, column=3, padx=(0, 6))
        self.plan_bar.bind("<Configure>", self._resize_plan_summary)
        self.token_usage_label = tk.Label(
            source, textvariable=self.token_usage_var, bg=PALE_BLUE, fg=BLUE,
            font=font(9), anchor="w", padx=12, pady=5)
        self.token_usage_label.grid(row=3, column=0, columnspan=3, sticky="ew",
                                    padx=16, pady=(0, 14))
        self.token_usage_label.grid_remove()

        workspace = tk.PanedWindow(outer, orient="horizontal", bg=BG, borderwidth=0,
                                   sashwidth=10, sashrelief="flat", showhandle=False)
        workspace.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        list_pane = tk.Frame(workspace, bg=BG)
        preview_pane = tk.Frame(workspace, bg=BG)
        detail_pane = tk.Frame(workspace, bg=BG)
        workspace.add(list_pane, minsize=330, width=450, stretch="always")
        workspace.add(preview_pane, minsize=350, width=550, stretch="always")
        workspace.add(detail_pane, minsize=260, width=360, stretch="always")
        self._build_pdf_list(list_pane)
        self._build_cover(preview_pane)
        self._build_outline(detail_pane)

        self.settings_window = tk.Toplevel(self.root, bg=WHITE)
        self.settings_window.withdraw()
        self.settings_window.title("处理设置")
        self.settings_window.transient(self.root)
        self.settings_window.resizable(False, False)
        self.settings_window.protocol("WM_DELETE_WINDOW", self._toggle_settings)
        self.settings_window.bind("<Escape>", lambda _event: self._toggle_settings())
        self.settings_body = tk.Frame(self.settings_window, bg=WHITE)
        self.settings_body.pack(fill="both", expand=True)
        self._build_settings(self.settings_body)

        footer = self._card(outer)
        footer.grid(row=3, column=0, sticky="ew")
        self.start_button = RoundedButton(footer, text="为此 PDF 添加书签", icon_name="play",
                                          kind="primary", width=220, height=40,
                                          command=self._start)
        self.start_button.pack(side="left", padx=(12, 0), pady=10)
        self.stop_button = RoundedButton(footer, text="停止", icon_name="stop",
                                         kind="secondary", width=104, height=40,
                                         command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0), pady=10)
        tk.Label(footer, image=self._icon("info", size=17), bg=WHITE).pack(side="left",
                                                                           padx=(18, 8))
        status = tk.Frame(footer, bg=WHITE)
        status.pack(side="left", fill="x", expand=True)
        tk.Label(status, textvariable=self.status_var, bg=WHITE, fg=TEXT,
                 font=font(10, bold=True), anchor="w").pack(fill="x")
        tk.Label(status, textvariable=self.report_var, bg=WHITE, fg=MUTED,
                 font=font(9), anchor="w").pack(fill="x")
        self.report_button = RoundedButton(footer, text="打开报告", icon_name="file-report",
                                           kind="secondary", width=122, height=40,
                                           command=self._open_report, state="disabled")
        self.report_button.pack(side="right", padx=(0, 12))
        self.log_button = RoundedButton(footer, text="查看日志", icon_name="file-text",
                                        kind="secondary", width=122, height=40,
                                        command=self._toggle_log)
        self.log_button.pack(side="right", padx=(0, 8))

    def _resize_plan_summary(self, event: tk.Event) -> None:
        # Wrap the run scope in whatever width the settings summary leaves.
        used = sum(widget.winfo_reqwidth() for widget in
                   (self.plan_icon, self.settings_summary_label, self.settings_toggle))
        self.run_summary_label.configure(wraplength=max(event.width - used - 46, 160))

    def _build_pdf_list(self, parent: tk.Misc) -> None:
        card = self._card(parent)
        card.pack(fill="both", expand=True)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(2, weight=1)
        heading = tk.Frame(card, bg=WHITE)
        heading.grid(row=0, column=0, sticky="ew", padx=15, pady=(11, 10))
        self._badge(heading, "pdf").pack(side="left", padx=(0, 10))
        title = tk.Frame(heading, bg=WHITE)
        title.pack(side="left")
        self._label(title, "PDF 清单", size=12, bold=True).pack(anchor="w")
        self._label(title, color=MUTED, size=9, textvariable=self.source_mode_var).pack(anchor="w")
        self._label(heading, color=MUTED, size=9, textvariable=self.count_var).pack(side="right")
        filter_bar = tk.Frame(card, bg=WHITE)
        filter_bar.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        filter_bar.grid_columnconfigure(0, weight=1)
        search_row = tk.Frame(filter_bar, bg=WHITE, highlightbackground=BORDER,
                              highlightthickness=1)
        search_row.grid(row=0, column=0, sticky="ew")
        search_row.grid_columnconfigure(1, weight=1)
        tk.Label(search_row, image=self._icon("search", size=16, color=MUTED), bg=WHITE).grid(
            row=0, column=0, padx=(10, 5), pady=4)
        self.search_entry = tk.Entry(search_row, textvariable=self.search_var, relief="flat",
                                     bg=WHITE, fg=TEXT, font=font(10), highlightthickness=0,
                                     insertbackground=TEXT)
        self.search_entry.grid(row=0, column=1, sticky="ew", pady=7)
        self.search_placeholder = tk.Label(search_row, text="筛选文件...", bg=WHITE, fg="#98A9C1",
                                            font=font(10), cursor="xterm")
        self.search_placeholder.place(x=36, rely=0.5, anchor="w")
        self.search_placeholder.bind("<Button-1>", lambda _event: self.search_entry.focus_set())
        self.search_entry.bind("<FocusIn>", self._update_search_placeholder)
        self.search_entry.bind("<FocusOut>", self._update_search_placeholder)
        self.search_entry.bind(
            "<FocusIn>", lambda _event: search_row.configure(highlightbackground=BLUE), add="+")
        self.search_entry.bind(
            "<FocusOut>", lambda _event: search_row.configure(highlightbackground=BORDER),
            add="+")
        self.status_filter = ttk.Combobox(filter_bar, textvariable=self.filter_var,
                                           values=("全部", "待处理", "跳过", "异常", "需复核", "失败"),
                                           width=7, state="readonly", style="App.TCombobox")
        self.status_filter.grid(row=0, column=1, padx=(8, 0), sticky="ns")
        tree_box = tk.Frame(card, bg=WHITE)
        tree_box.grid(row=2, column=0, sticky="nsew", padx=(14, 14))
        tree_box.grid_rowconfigure(0, weight=1)
        tree_box.grid_columnconfigure(0, weight=1)
        self.pdf_tree = ttk.Treeview(tree_box, style="PdfList.Card.Treeview", selectmode="browse",
                                     columns=("file", "pages", "bookmarks", "action"),
                                     displaycolumns=("file", "pages", "action"), show="tree headings")
        self.pdf_tree.heading("#0", text="序号")
        self.pdf_tree.heading("file", text="文件", anchor="w")
        self.pdf_tree.heading("pages", text="页")
        self.pdf_tree.heading("bookmarks", text="原书签")
        self.pdf_tree.heading("action", text="计划/结果")
        self.pdf_tree.column("#0", width=62, minwidth=56, stretch=False, anchor="center")
        self.pdf_tree.column("file", width=170, minwidth=100, stretch=True)
        self.pdf_tree.column("pages", width=45, minwidth=40, stretch=False, anchor="center")
        self.pdf_tree.column("bookmarks", width=65, minwidth=58, stretch=False, anchor="center")
        self.pdf_tree.column("action", width=86, minwidth=76, stretch=False, anchor="center")
        self.pdf_tree.grid(row=0, column=0, sticky="nsew")
        self.pdf_tree.tag_configure("skipped", foreground=MUTED)
        self.pdf_tree.tag_configure("review", foreground="#975416")
        self.pdf_tree.tag_configure("failed", foreground=FAILURE,
                                    background=FAILURE_PALE)
        scrollbar = ttk.Scrollbar(tree_box, orient="vertical", style="App.Vertical.TScrollbar",
                                  command=self.pdf_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.pdf_tree.configure(yscrollcommand=scrollbar.set)
        self.pdf_tree.bind("<<TreeviewSelect>>", self._on_pdf_selected)
        self.pdf_tree.bind("<Double-1>", self._open_selected_pdf)
        self.pdf_tree.bind("<Return>", self._open_selected_pdf)
        hint = tk.Frame(card, bg=COLORS.surface_tint)
        hint.grid(row=3, column=0, sticky="ew", padx=14, pady=(10, 12))
        tk.Label(hint, image=self._icon("info", size=15, color=MUTED),
                 bg=COLORS.surface_tint).pack(side="left", anchor="n", padx=(9, 7), pady=8)
        self.list_hint_label = WrapLabel(hint, textvariable=self.list_hint_var,
                                         bg=COLORS.surface_tint, fg=MUTED,
                                         font=font(9), anchor="w", wraplength=340,
                                         justify="left", pady=7)
        self.list_hint_label.pack(side="left", fill="x", expand=True)
        hint.bind("<Configure>", lambda event: self.list_hint_label.configure(
            wraplength=max(event.width - 44, 120)))
        self._pdf_icon = self._icon("pdf", size=16, color="#DA343C")

    def _update_search_placeholder(self, _event: object = None) -> None:
        if not hasattr(self, "search_placeholder"):
            return
        if self.search_var.get() or self.search_entry.focus_get() is self.search_entry:
            self.search_placeholder.place_forget()
        else:
            self.search_placeholder.place(x=36, rely=0.5, anchor="w")

    def _build_cover(self, parent: tk.Misc) -> None:
        card = self._card(parent)
        card.pack(fill="both", expand=True)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(3, weight=1)
        heading = tk.Frame(card, bg=WHITE)
        heading.grid(row=0, column=0, sticky="ew", padx=15, pady=(11, 7))
        self._badge(heading, "eye").pack(side="left", padx=(0, 10))
        self._label(heading, "PDF 页面预览", size=12, bold=True).pack(side="left")
        self.preview_title_label = WrapLabel(
            card, textvariable=self.preview_title_var, bg=WHITE, fg=TEXT,
            font=font(10, bold=True), anchor="w", justify="left", wraplength=390)
        self.preview_title_label.grid(row=1, column=0, sticky="ew", padx=15)
        self.preview_meta_label = WrapLabel(
            card, textvariable=self.preview_meta_var, bg=WHITE, fg=MUTED, font=font(9),
            anchor="w", wraplength=390, justify="left")
        self.preview_meta_label.grid(row=2, column=0, sticky="ew", padx=15, pady=(3, 9))
        self.cover_canvas = tk.Canvas(card, bg=COLORS.preview, highlightthickness=0)
        self.cover_canvas.grid(row=3, column=0, sticky="nsew", padx=14, pady=(0, 6))
        self.cover_canvas.bind("<Configure>", self._schedule_cover_redraw)
        self.cover_canvas.bind("<ButtonPress-1>", lambda event: self.cover_canvas.scan_mark(event.x, event.y))
        self.cover_canvas.bind("<B1-Motion>", lambda event: self.cover_canvas.scan_dragto(event.x, event.y, gain=1))
        toolbar = tk.Frame(card, bg=WHITE)
        toolbar.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 9))
        self.prev_button = RoundedButton(toolbar, text="", icon_name="left", width=36,
                                          height=34, command=lambda: self._step_page(-1))
        self.prev_button.pack(side="left")
        page_box = tk.Frame(toolbar, bg=WHITE)
        page_box.pack(side="left", padx=(7, 7))
        self.page_entry = ttk.Entry(page_box, textvariable=self.page_var, width=4,
                                    justify="center", style="App.TEntry")
        self.page_entry.pack(side="left")
        self.page_entry.bind("<Return>", self._on_page_entered)
        self.page_entry.bind("<FocusOut>", self._on_page_entered)
        self._label(page_box, textvariable=self.page_total_var, color=MUTED, size=9).pack(
            side="left", padx=(3, 0))
        self.next_button = RoundedButton(toolbar, text="", icon_name="right", width=36,
                                          height=34, command=lambda: self._step_page(1))
        self.next_button.pack(side="left")
        self.fullscreen_button = RoundedButton(toolbar, text="", icon_name="expand", width=36,
                                                height=34, command=self._open_fullscreen)
        self.fullscreen_button.pack(side="right")
        self.zoom_in_button = RoundedButton(toolbar, text="", icon_name="zoom-in", width=36,
                                             height=34, command=lambda: self._step_zoom(25))
        self.zoom_in_button.pack(side="right", padx=(5, 5))
        self.zoom_box = ttk.Combobox(toolbar, textvariable=self.zoom_var, width=5,
                                     values=("50%", "75%", "100%", "125%", "150%", "200%", "250%"),
                                     state="readonly", justify="center", style="App.TCombobox")
        self.zoom_box.pack(side="right")
        self.zoom_box.bind("<<ComboboxSelected>>", self._on_zoom_selected)
        self.zoom_out_button = RoundedButton(toolbar, text="", icon_name="zoom-out", width=36,
                                              height=34, command=lambda: self._step_zoom(-25))
        self.zoom_out_button.pack(side="right", padx=(5, 5))
        card.bind("<Configure>", self._resize_cover_labels)

    def _resize_cover_labels(self, event: tk.Event) -> None:
        width = max(event.width - 32, 150)
        if width == self._cover_label_wrap_width:
            return
        self._cover_label_wrap_width = width
        self.preview_title_label.configure(wraplength=width)
        self.preview_meta_label.configure(wraplength=width)

    def _build_outline(self, parent: tk.Misc) -> None:
        card = self._card(parent)
        card.pack(fill="both", expand=True)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(2, weight=1)
        heading = tk.Frame(card, bg=WHITE)
        heading.grid(row=0, column=0, sticky="ew", padx=15, pady=(11, 8))
        self._badge(heading, "bookmark").pack(side="left", padx=(0, 10))
        self._label(heading, "书签与结果", size=12, bold=True).pack(side="left")
        self._label(heading, color=MUTED, size=9, textvariable=self.outline_count_var).pack(
            side="right")
        self.outline_action_label = WrapLabel(
            card, textvariable=self.outline_action_var, bg=COLORS.notice, fg=MUTED,
            font=font(9), anchor="w", justify="left", wraplength=220,
            padx=10, pady=7)
        self.outline_action_label.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        self.detail_tabs = ttk.Notebook(card, style="Card.TNotebook")
        self.detail_tabs.grid(row=2, column=0, sticky="nsew", padx=14)
        existing_tab = tk.Frame(self.detail_tabs, bg=WHITE)
        result_tab = tk.Frame(self.detail_tabs, bg=WHITE)
        self.log_tab = tk.Frame(self.detail_tabs, bg=WHITE)
        self.detail_tabs.add(existing_tab, text="现有书签")
        self.detail_tabs.add(result_tab, text="识别结果")
        self.detail_tabs.add(self.log_tab, text="日志")
        self.detail_tabs.bind("<<NotebookTabChanged>>", self._on_detail_tab_changed)
        tree_box = tk.Frame(existing_tab, bg=WHITE)
        tree_box.pack(fill="both", expand=True)
        tree_box.grid_rowconfigure(0, weight=1)
        tree_box.grid_columnconfigure(0, weight=1)
        self.outline_tree = ttk.Treeview(tree_box, style="Card.Treeview", show="tree headings",
                                         columns=("title", "page"), selectmode="browse")
        self.outline_tree.heading("#0", text="#")
        self.outline_tree.heading("title", text="标题", anchor="w")
        self.outline_tree.heading("page", text="页码")
        self.outline_tree.column("#0", width=40, minwidth=36, stretch=False, anchor="center")
        self.outline_tree.column("title", width=150, minwidth=110, stretch=True)
        self.outline_tree.column("page", width=52, minwidth=46, stretch=False, anchor="center")
        self.outline_tree.grid(row=0, column=0, sticky="nsew")
        self.outline_tree.tag_configure("alternate", background=COLORS.surface_tint)
        scrollbar = ttk.Scrollbar(tree_box, orient="vertical", style="App.Vertical.TScrollbar",
                                  command=self.outline_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.outline_tree.configure(yscrollcommand=scrollbar.set)
        self.outline_tree.bind("<<TreeviewSelect>>", self._on_outline_selected)
        result_tab.grid_columnconfigure(0, weight=1)
        result_tab.grid_rowconfigure(1, weight=1)
        result_header = tk.Frame(result_tab, bg=WHITE)
        result_header.grid(row=0, column=0, sticky="ew", pady=(6, 6))
        self._label(result_header, color=MUTED, size=9,
                    textvariable=self.result_count_var).pack(side="left")
        self.toc_button = ttk.Button(result_header, text="目录页", style="App.TButton",
                                      command=self._jump_to_toc, state="disabled")
        self.toc_button.pack(side="right")
        result_box = tk.Frame(result_tab, bg=WHITE)
        result_box.grid(row=1, column=0, sticky="nsew")
        result_box.grid_columnconfigure(0, weight=1)
        result_box.grid_rowconfigure(0, weight=1)
        self.result_tree = ttk.Treeview(result_box, style="Card.Treeview", show="tree headings",
                                         columns=("title", "page"), selectmode="browse")
        self.result_tree.heading("#0", text="#")
        self.result_tree.heading("title", text="识别标题", anchor="w")
        self.result_tree.heading("page", text="PDF 页")
        self.result_tree.column("#0", width=42, minwidth=38, stretch=False, anchor="center")
        self.result_tree.column("title", width=130, minwidth=100, stretch=True)
        self.result_tree.column("page", width=60, minwidth=54, stretch=False, anchor="center")
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        self.result_tree.bind("<<TreeviewSelect>>", self._on_result_selected)
        result_scroll = ttk.Scrollbar(result_box, orient="vertical", style="App.Vertical.TScrollbar",
                                       command=self.result_tree.yview)
        result_scroll.grid(row=0, column=1, sticky="ns")
        self.result_tree.configure(yscrollcommand=result_scroll.set)
        self.result_detail_label = WrapLabel(
            result_tab, textvariable=self.result_detail_var, bg=WHITE, fg=MUTED,
            font=font(9), anchor="w", justify="left", wraplength=230)
        self.result_detail_label.grid(row=2, column=0, sticky="ew", pady=(8, 5))
        self.open_result_button = ttk.Button(result_tab, text="打开生成的 PDF",
                                              style="App.TButton", command=self._open_result_pdf,
                                              state="disabled")
        self.open_result_button.grid(row=3, column=0, sticky="e", pady=(0, 5))
        self.log = tk.Text(self.log_tab, height=8, wrap="word", state="disabled", relief="flat",
                           bg=COLORS.surface_tint, fg=TEXT, font=font(9), padx=10, pady=8,
                           highlightthickness=0, borderwidth=0)
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure(
            "book_progress", foreground="#174A82", background="#E7F0FF",
            font=font(11, bold=True), spacing1=8, spacing3=4)
        self.log.tag_configure(
            "failed_progress", foreground=FAILURE,
            background=FAILURE_PALE, font=font(11, bold=True))
        self.log.tag_configure(
            "failed_result", foreground=FAILURE,
            background=FAILURE_PALE, font=font(9, bold=True))
        self.outline_hint_label = WrapLabel(
            card, textvariable=self.outline_hint_var, bg=WHITE, fg=MUTED, font=font(9),
            anchor="w", justify="left", wraplength=220)
        self.outline_hint_label.grid(row=3, column=0, sticky="ew", padx=14, pady=(9, 11))
        card.bind("<Configure>", self._resize_outline_labels)

    def _resize_outline_labels(self, event: tk.Event) -> None:
        width = max(event.width - 48, 120)
        if width == self._outline_label_wrap_width:
            return
        self._outline_label_wrap_width = width
        self.outline_action_label.configure(wraplength=width)
        self.outline_hint_label.configure(wraplength=width)
        self.result_detail_label.configure(wraplength=width)

    def _settings_section(self, parent: tk.Misc, title: str, hint: str = "") -> tk.Frame:
        section = tk.Frame(parent, bg=WHITE)
        section.pack(fill="x", padx=24, pady=(18, 0))
        heading = tk.Frame(section, bg=WHITE)
        heading.pack(fill="x", pady=(0, 4))
        self._label(heading, title, bold=True).pack(side="left")
        if hint:
            self._label(heading, hint, color=MUTED, size=9).pack(side="left", padx=(10, 0))
        return section

    def _settings_choice(self, parent: tk.Misc, widget: ttk.Widget, description: str,
                         *, indent: int = 0) -> None:
        widget.pack(anchor="w", padx=(indent, 0), pady=(6, 0))
        self._label(parent, description, color=MUTED, size=9, anchor="w",
                    justify="left").pack(anchor="w", padx=(indent + 24, 0))

    def _build_settings(self, card: tk.Frame) -> None:
        section = self._settings_section(card, "DeepSeek API Key",
                                         "按当前 Windows 用户加密，保存在软件 data 文件夹")
        field = tk.Frame(section, bg=WHITE, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=BLUE)
        field.pack(fill="x", pady=(2, 0))
        field.grid_columnconfigure(0, weight=1)
        self.api_key_entry = ttk.Entry(field, textvariable=self.api_key_var, show="*",
                                       width=46, style="Field.TEntry")
        self.api_key_entry.grid(row=0, column=0, sticky="ew")
        self.api_key_entry.bind("<FocusOut>", self._persist_api_key)
        self.api_key_entry.bind(
            "<FocusIn>", lambda _event: field.configure(highlightbackground=BLUE), add="+")
        self.api_key_entry.bind(
            "<FocusOut>", lambda _event: field.configure(highlightbackground=BORDER), add="+")
        self._show_api_key = False
        self.api_eye_button = ttk.Button(field, image=self._icon("eye", size=16, color=MUTED),
                                          style="Icon.TButton", command=self._toggle_api_key)
        self.api_eye_button.grid(row=0, column=1, padx=(0, 3))

        section = self._settings_section(card, "处理规则", "三选一")
        self.policy_buttons = []
        for value, label, description in (
                ("verify", "核对并修正（推荐）",
                 "识别印刷目录后核对现有书签；一致则跳过，不一致且识别可靠时替换"),
                ("skip", "只处理无书签", "已有书签的 PDF 直接跳过，不调用 DeepSeek"),
                ("replace", "强制重建", "忽略现有书签，按印刷目录重新生成")):
            button = ttk.Radiobutton(section, text=label, value=value,
                                      variable=self.policy_var, command=self._on_policy_changed,
                                      style="App.TRadiobutton")
            self._settings_choice(section, button, description)
            self.policy_buttons.append(button)
        self.compare_on_replace_checkbox = ttk.Checkbutton(
            section, text="重建时也记录差异", variable=self.compare_on_replace_var,
            command=self._on_policy_changed, style="App.TCheckbutton", state="disabled")
        self._settings_choice(section, self.compare_on_replace_checkbox,
                              "在报告中记录旧书签与新目录的差异", indent=24)

        section = self._settings_section(card, "运行方式", "可同时勾选")
        self.dry_checkbox = ttk.Checkbutton(section, text="仅分析（仍调用 API）",
                                             variable=self.dry_run_var, style="App.TCheckbutton")
        self._settings_choice(section, self.dry_checkbox,
                              "只识别并写入报告，不生成 PDF")
        self.resume_checkbox = ttk.Checkbutton(section, text="续跑：跳过已完成的书",
                                                variable=self.resume_var, style="App.TCheckbutton")
        self._settings_choice(section, self.resume_checkbox,
                              "文件和选项未变、上次已完成的书不再处理")
        self.overwrite_original_checkbox = ttk.Checkbutton(
            section, text="直接覆盖原 PDF（仅成功时替换）",
            variable=self.overwrite_original_var, style="App.TCheckbutton")
        self._settings_choice(section, self.overwrite_original_checkbox,
                              "校验通过后在原位置替换；需复核或失败的文件保持不变")

        footer = tk.Frame(card, bg=COLORS.notice)
        footer.pack(fill="x", pady=(22, 0))
        tk.Label(footer, image=self._icon("info", size=16), bg=COLORS.notice).pack(
            side="left", padx=(24, 8), pady=14)
        tk.Label(footer, text="预览在本机完成；开始处理或仅分析会上传识别页面至 DeepSeek，"
                 "并可能产生费用。", bg=COLORS.notice, fg=BLUE, font=font(9),
                 justify="left", wraplength=400).pack(side="left", fill="x", expand=True)
        RoundedButton(footer, text="完成", kind="primary", width=88, height=34,
                      command=self._toggle_settings).pack(side="right", padx=(12, 24))

    def _toggle_api_key(self) -> None:
        self._show_api_key = not self._show_api_key
        self.api_key_entry.configure(show="" if self._show_api_key else "*")

    def _focus_api_key(self) -> None:
        if not self._settings_expanded:
            self._toggle_settings()
        self.api_key_entry.focus_set()

    def _toggle_settings(self) -> None:
        self._settings_expanded = not self._settings_expanded
        if self._settings_expanded:
            self.settings_window.update_idletasks()
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            width = min(max(560, self.settings_body.winfo_reqwidth()),
                        screen_width - 40)
            height = min(max(190, self.settings_body.winfo_reqheight()),
                         screen_height - 80)
            x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
            x = max(20, min(x, screen_width - width - 20))
            y = max(20, min(y, screen_height - height - 60))
            self.settings_window.geometry(f"{width}x{height}+{x}+{y}")
            self.settings_window.deiconify()
            self.settings_window.lift(self.root)
            self.settings_window.focus_set()
            self.settings_toggle.configure(text="收起设置")
        else:
            self.settings_window.withdraw()
            self.settings_toggle.configure(text="处理设置")

    def _update_settings_summary(self) -> None:
        if self.skip_bookmarked_var.get():
            policy = "只处理无书签"
        elif self.replace_var.get():
            policy = "强制重建并核对" if self.verify_var.get() else "强制重建"
        else:
            policy = "核对并修正" if self.verify_var.get() else "按原书签判断"
        destination = "覆盖原 PDF" if self.overwrite_original_var.get() else "保存副本"
        action = "仅分析" if self.dry_run_var.get() else "生成 PDF"
        self.settings_summary_var.set(f"{policy} · {destination} · {action}")

    def _persist_api_key(self, _event: object = None) -> None:
        value = self.api_key_var.get().strip()
        if not value:
            # Clearing the field while editing must never erase an API Key
            # saved in the application's data directory.
            if self._cached_api_key:
                self.api_key_var.set(self._cached_api_key)
            return
        if value == self._cached_api_key:
            return
        try:
            save_key(value)
        except (KeyCacheError, OSError) as error:
            self._append(f"无法缓存 API Key：{error}")
            return
        self._cached_api_key = value

    def _update_api_key_controls(self) -> None:
        state = "disabled" if self._running else "normal"
        self.api_key_entry.configure(state=state)
        self.api_eye_button.configure(state=state)

    def _on_policy_changed(self) -> None:
        if self._running:
            return
        policy = self.policy_var.get()
        self._updating_policy = True
        try:
            self.skip_bookmarked_var.set(policy == "skip")
            self.replace_var.set(policy == "replace")
            self.verify_var.set(policy == "verify" or
                                (policy == "replace" and self.compare_on_replace_var.get()))
        finally:
            self._updating_policy = False
        self.compare_on_replace_checkbox.configure(
            state="normal" if policy == "replace" else "disabled")
        self._on_replace_changed()

    def _on_replace_changed(self, *_: object) -> None:
        if not self._running and not self._updating_policy:
            self._post_run_message = None
            self._clear_run_results()
            self._ensure_resume_report()
            self._refresh_plan()
            self._update_settings_summary()

    def _ensure_resume_report(self) -> None:
        if (not self.resume_var.get() or self._source_path is None
                or self._resume_done_loaded or self._resume_load_pending
                or self._current_signature() != self._loaded_signature):
            return
        self._resume_load_pending = True
        self._resume_load_error = ""
        generation = self._scan_generation
        output = self._resolved_output_dir()
        source = self._source_path
        assert source is not None
        threading.Thread(target=self._load_resume_report,
                         args=(generation, source, output, self.overwrite_original_var.get()),
                         daemon=True).start()

    def _load_resume_report(self, generation: int, source: Path, output: Path,
                            overwrite: bool) -> None:
        try:
            done = _done_files(job_data_dir(source, output, overwrite) /
                               "bookmarker-report.jsonl")
            self._events.put(("resume_done", (generation, done, "")))
        except (OSError, UnicodeError) as error:
            self._events.put(("resume_done", (generation, {}, str(error))))

    def _on_overwrite_changed(self, *_: object) -> None:
        self._update_output_controls()
        if not self._running:
            self._load_source(force=True)
            self._update_settings_summary()

    def _update_output_controls(self) -> None:
        overwrite = self.overwrite_original_var.get()
        self.output_label.configure(text="输出（覆盖时不使用）" if overwrite else "输出")
        state = "disabled" if self._running or overwrite else "normal"
        for widget in (self.output_entry, self.output_button, self.clear_output_button):
            widget.configure(state=state)

    def _resolved_output_dir(self) -> Path:
        if self.overwrite_original_var.get():
            return (self.working_dir / "output").resolve()
        value = self.output_var.get().strip()
        return Path(value).expanduser().resolve() if value else (self.working_dir / "output").resolve()

    def _on_path_committed(self, _event: object = None) -> None:
        if not self._running:
            self._load_source()

    def _current_signature(self) -> tuple[object, ...]:
        overwrite = self.overwrite_original_var.get()
        return (self.input_var.get().strip(),
                "" if overwrite else self.output_var.get().strip(), overwrite)

    def _pick_input(self) -> None:
        initial = Path(self.input_var.get()).expanduser()
        if initial.is_file():
            initial = initial.parent
        if not initial.is_dir():
            initial = self.working_dir
        chosen = filedialog.askdirectory(initialdir=str(initial), title="选择 PDF 所在文件夹")
        if chosen:
            previous_suggestion = self._suggested_output
            self.input_var.set(chosen)
            self._suggested_output = str(self.working_dir / "output")
            if not self.output_var.get().strip() or self.output_var.get() == previous_suggestion:
                self.output_var.set(self._suggested_output)
            self._load_source(force=True)

    def _pick_file(self) -> None:
        initial = Path(self.input_var.get()).expanduser()
        if initial.is_file():
            initial = initial.parent
        if not initial.is_dir():
            initial = self.working_dir
        chosen = filedialog.askopenfilename(
            initialdir=str(initial), title="选择单个 PDF",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        if chosen:
            previous_suggestion = self._suggested_output
            self.input_var.set(chosen)
            self._suggested_output = str(self.working_dir / "output")
            if not self.output_var.get().strip() or self.output_var.get() == previous_suggestion:
                self.output_var.set(self._suggested_output)
            self._load_source(force=True)

    def _pick_output(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.output_var.get(), title="选择输出文件夹")
        if chosen:
            self.output_var.set(chosen)
            self._load_source(force=True)

    def _clear_input(self) -> None:
        if not self._running:
            self.input_var.set("")
            self._load_source(force=True)

    def _clear_output(self) -> None:
        if not self._running:
            self.output_var.set("")
            self._load_source(force=True)

    def _load_source(self, *, force: bool = False, keep_results: bool = False,
                     source_changed: bool = False) -> None:
        input_text = self.input_var.get().strip()
        output_text = self.output_var.get().strip()
        signature = self._current_signature()
        if not force and signature == self._loaded_signature:
            return
        self._post_run_message = ((self.status_var.get(), self.run_summary_var.get())
                                  if keep_results else None)
        self._source_changed_before_run = source_changed
        self._loaded_signature = signature
        self._scan_cancel.set()
        self._scan_cancel = threading.Event()
        self._scan_generation += 1
        generation = self._scan_generation
        self._render_generation += 1
        self._scanning = False
        self._source_path = None
        self._source_is_file = False
        self._previews.clear()
        self._base_previews.clear()
        self._file_indices.clear()
        self._scan_fingerprints.clear()
        self._resume_done.clear()
        self._resume_done_loaded = False
        self._resume_load_pending = self.resume_var.get()
        self._resume_load_error = ""
        self._scan_total = 0
        self._cover_cache.clear()
        self.pdf_tree.delete(*self.pdf_tree.get_children())
        self._clear_preview()
        if not keep_results:
            self._clear_run_results()
        self._scan_skipped = 0
        self._scan_failed = 0
        self._scan_resumed = 0

        overwrite = self.overwrite_original_var.get()
        if not input_text or (not overwrite and not output_text):
            self._resume_load_pending = False
            self.count_var.set("尚未选择 PDF")
            self.source_mode_var.set("选择 PDF 或文件夹")
            self.list_hint_var.set("选择单个 PDF 或文件夹")
            self.status_var.set("请选择来源" if overwrite else "请选择来源和输出位置")
            self.run_summary_var.set("选择 PDF 或文件夹后显示本次范围；预览不会调用 DeepSeek")
            self._update_start_state()
            return
        source = Path(input_text).expanduser().resolve()
        output = self._resolved_output_dir()
        if source.is_file() and source.suffix.lower() == ".pdf":
            self._source_is_file = True
            self.source_mode_var.set("单个 PDF")
        elif source.is_dir():
            self._source_is_file = False
            self.source_mode_var.set("文件夹")
        else:
            self._resume_load_pending = False
            self.count_var.set("未找到 PDF")
            self.source_mode_var.set("无效来源")
            self.list_hint_var.set("检查输入路径是否指向 PDF 文件或文件夹")
            self.status_var.set("输入路径无效")
            self.run_summary_var.set("请检查来源路径")
            self._update_start_state()
            return
        if not overwrite and not self._source_is_file and (
            source.is_relative_to(output) or output.is_relative_to(source)
        ):
            self._resume_load_pending = False
            self.count_var.set("输出位置无效")
            self.list_hint_var.set("输入和输出文件夹不能相同，也不能互相包含")
            self.status_var.set("请更换输出位置")
            self.run_summary_var.set("输入和输出文件夹不能互相包含")
            self._update_start_state()
            return
        self._source_path = source
        self._scanning = True
        self.count_var.set("正在读取 PDF…")
        self.list_hint_var.set("正在检查页数和现有书签")
        self.status_var.set("扫描中…")
        self.run_summary_var.set("正在本地扫描 PDF；此步骤不会调用 DeepSeek")
        self._update_start_state()
        threading.Thread(target=self._scan_source, args=(generation, source, output,
                         self._source_is_file, self.resume_var.get(), overwrite,
                         self._scan_cancel),
                         daemon=True).start()

    def _scan_source(self, generation: int, source: Path, output: Path, single: bool,
                     resume: bool, overwrite: bool, cancel: threading.Event) -> None:
        try:
            if not overwrite:
                migrate_legacy_job_data(output, job_data_dir(source, output))
            paths = [source] if single else list_pdf_paths(
                source, None if overwrite else output)
            done = None
            resume_error = ""
            if resume:
                try:
                    done = _done_files(job_data_dir(source, output, overwrite) /
                                       "bookmarker-report.jsonl")
                except (OSError, UnicodeError) as error:
                    resume_error = str(error)
            self._events.put(("scan_total", (generation, len(paths), done, resume_error)))
            for index, path in enumerate(paths, 1):
                if cancel.is_set():
                    return
                fingerprint = _file_fingerprint(path)
                preview = inspect_pdf(path, verify_existing=True, include_bookmarks=single)
                self._events.put(("scan_item", (generation, index, preview, fingerprint)))
            self._events.put(("scan_done", generation))
        except LegacyDataMigrationError:
            self._events.put(("scan_error", (generation,
                "无法整理旧版输出目录中的报告和缓存；请检查文件是否被占用，或更换输出位置。")))
        except Exception as error:
            self._events.put(("scan_error", (generation, str(error))))

    def _handle_scan_item(self, generation: int, index: int, preview: PdfPreview,
                          fingerprint: tuple[int, int] | None) -> None:
        if generation != self._scan_generation:
            return
        path_text = str(preview.path)
        self._base_previews[path_text] = preview
        classified = self._classify_preview(preview)
        self._previews[path_text] = classified
        self._file_indices[path_text] = index
        self._scan_fingerprints[path_text] = fingerprint
        if self._matches_filter(path_text, classified):
            self._insert_pdf_row(path_text, classified)
            if not self.pdf_tree.selection():
                self.pdf_tree.selection_set(path_text)
                self._sync_pdf_selection_style()
                self.pdf_tree.focus(path_text)
                self._show_preview(classified)
        self._update_plan_summary()

    def _classify_preview(self, base: PdfPreview) -> PdfPreview:
        preview = reclassify_preview(
            base, replace_existing=self.replace_var.get(),
            verify_existing=self.verify_var.get(),
            skip_bookmarked=self.skip_bookmarked_var.get())
        if self.resume_var.get() and self._source_path is not None:
            source_dir = self._source_path.parent if self._source_is_file else self._source_path
            if _resume_skips(
                base.path, source_dir, self._resolved_output_dir(),
                self._resume_done.get(str(base.path)), dry_run=self.dry_run_var.get(),
                replace_existing=self.replace_var.get(), verify_existing=self.verify_var.get(),
                skip_bookmarked=self.skip_bookmarked_var.get(),
                overwrite_original=self.overwrite_original_var.get()):
                preview = replace(preview, eligible=False, reason="续跑已完成，默认跳过")
        return preview

    def _file_label(self, preview: PdfPreview) -> str:
        if self._source_path and not self._source_is_file:
            try:
                return str(preview.path.relative_to(self._source_path))
            except ValueError:
                pass
        return preview.path.name

    def _row_action(self, path_text: str, preview: PdfPreview) -> str:
        status = self._run_status.get(path_text)
        if status:
            return {
                "queued": "排队中", "running": "处理中", "success": "已完成",
                "dry_run": "已分析", "skipped": "跳过", "resume_skip": "续跑跳过",
                "needs_review": "需复核", "failed": "失败",
                "interrupted": "已中止", "unprocessed": "未处理",
            }.get(status, status)
        if preview.reason.startswith("续跑"):
            return "续跑跳过"
        return _processing_label(preview, replace_existing=self.replace_var.get(),
                                 verify_existing=self.verify_var.get())

    def _matches_filter(self, path_text: str, preview: PdfPreview) -> bool:
        query = self.search_var.get().strip().casefold()
        if query and query not in self._file_label(preview).casefold():
            return False
        selected = self.filter_var.get()
        status = self._run_status.get(path_text)
        if selected == "待处理":
            return preview.eligible and status not in {
                "success", "dry_run", "skipped", "resume_skip", "needs_review", "failed"}
        if selected == "跳过":
            return status in {"skipped", "resume_skip"} or (
                status is None and not preview.eligible and preview.page_count > 0)
        if selected == "异常":
            return preview.page_count <= 0
        if selected == "需复核":
            return status == "needs_review"
        if selected == "失败":
            return status == "failed"
        return True

    def _insert_pdf_row(self, path_text: str, preview: PdfPreview) -> None:
        action = self._row_action(path_text, preview)
        tag = ("failed" if action in {"失败", "无法读取"} else
               "review" if action == "需复核" else
               "skipped" if action in {"跳过", "续跑跳过"} else "normal")
        self.pdf_tree.insert("", "end", iid=path_text, text=str(self._file_indices[path_text]),
                             image=self._pdf_icon,
                             values=(self._file_label(preview), preview.page_count or "—",
                                     preview.bookmark_count or "—", action), tags=(tag,))

    def _update_plan_summary(self) -> None:
        total = len(self._previews)
        ready = sum(preview.eligible for preview in self._previews.values())
        failed = sum(preview.page_count <= 0 for preview in self._previews.values())
        resumed = sum(preview.reason.startswith("续跑") for preview in self._previews.values())
        skipped = total - ready - failed - resumed
        self._scan_skipped, self._scan_failed, self._scan_resumed = skipped, failed, resumed
        self.count_var.set(f"已扫描 {total}/{self._scan_total}" if self._scanning else f"共 {total} 本")
        if total:
            scope = "这本 PDF" if self._source_is_file else f"整个文件夹的 {total} 本 PDF"
            suffix = "；仅分析仍会调用 API" if self.dry_run_var.get() else ""
            self.run_summary_var.set(
                f"运行范围：{scope}  ·  预计识别 {ready}  ·  跳过 {skipped + resumed}"
                f"  ·  无法读取 {failed}{suffix}"
                + ("  ·  正在读取续跑记录" if self.resume_var.get() and self._resume_load_pending
                   else "  ·  续跑记录读取失败" if self.resume_var.get() and self._resume_load_error
                   else ""))
        self._update_start_state()

    def _refresh_plan(self) -> None:
        if not self._base_previews:
            self._update_start_state()
            return
        self._previews = {path: self._classify_preview(base)
                          for path, base in self._base_previews.items()}
        self._rebuild_list()
        self._update_plan_summary()
        if self._displayed_path is not None:
            preview = self._previews.get(str(self._displayed_path))
            if preview is not None:
                self._set_action_detail(preview)

    def _clear_run_results(self) -> None:
        self._run_report = None
        self._run_results.clear()
        self._run_status.clear()
        self._logged_results.clear()
        self.token_usage_var.set("")
        self.token_usage_label.grid_remove()
        self.plan_bar.grid_configure(pady=(0, 14))
        if hasattr(self, "result_tree"):
            self.result_tree.delete(*self.result_tree.get_children())
            self.result_detail_var.set("处理后可在此查看识别书签和复核原因")
            self.result_count_var.set("尚无本次结果")
            self.toc_button.configure(state="disabled")
            self.open_result_button.configure(state="disabled")

    def _rebuild_list(self) -> None:
        if not hasattr(self, "pdf_tree"):
            return
        selected = self.pdf_tree.selection()
        previous = selected[0] if selected else None
        self.pdf_tree.delete(*self.pdf_tree.get_children())
        for path_text, preview in self._previews.items():
            if self._matches_filter(path_text, preview):
                self._insert_pdf_row(path_text, preview)
        if previous and self.pdf_tree.exists(previous):
            self.pdf_tree.selection_set(previous)
            self.pdf_tree.focus(previous)
        elif self.pdf_tree.get_children():
            first = self.pdf_tree.get_children()[0]
            self.pdf_tree.selection_set(first)
            self.pdf_tree.focus(first)
            self._show_preview(self._previews[first])
        else:
            self._clear_preview()
        self._sync_pdf_selection_style()
        self._update_start_state()

    def _sync_pdf_selection_style(self) -> None:
        """Keep a selected failed book red despite ttk's selection color map."""
        selected = self.pdf_tree.selection()
        failed = bool(selected and self.pdf_tree.exists(selected[0]) and
                      "failed" in self.pdf_tree.item(selected[0], "tags"))
        self._style.map(
            "PdfList.Card.Treeview",
            background=[("selected", COLORS.failure_selection if failed
                         else COLORS.blue_selection)],
            foreground=[("selected", FAILURE if failed else TEXT)],
        )

    def _on_pdf_selected(self, _event: object = None) -> None:
        self._sync_pdf_selection_style()
        selected = self.pdf_tree.selection()
        if selected and selected[0] in self._previews:
            preview = self._previews[selected[0]]
            if preview.path != self._displayed_path:
                self._show_preview(preview)

    def _open_selected_pdf(self, event: tk.Event | None = None) -> None:
        if event is not None and getattr(event, "num", None) == 1:
            item = self.pdf_tree.identify_row(event.y)
        else:
            selected = self.pdf_tree.selection()
            item = selected[0] if selected else ""
        if not item or item not in self._previews:
            return
        path = self._previews[item].path
        if not path.is_file():
            messagebox.showerror("无法打开 PDF", f"找不到原文件：\n{path}", parent=self.root)
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as error:
            messagebox.showerror("无法打开 PDF", f"请检查本机是否安装了 PDF 阅读器。\n{error}",
                                 parent=self.root)

    def _show_preview(self, preview: PdfPreview) -> None:
        self._displayed_path = preview.path
        self._selection_generation += 1
        selection_generation = self._selection_generation
        self._page_number = 1
        self._page_count = preview.page_count
        self.page_var.set("1")
        self.page_total_var.set(f"/ {preview.page_count}")
        self._zoom_factor = 1.0
        self.zoom_var.set("100%")
        self._update_page_controls()
        self.preview_title_var.set(self._file_label(preview))
        self.preview_meta_var.set(
            f"{preview.page_count} 页  ·  {_file_size(preview.path)}  ·  "
            f"现有 {preview.bookmark_count} 个书签")
        self.outline_count_var.set(f"共 {preview.bookmark_count} 条")
        self._set_action_detail(preview)
        self._populate_result(preview.path)
        self._populate_outline(preview.bookmarks)
        if preview.bookmark_count and not preview.bookmarks:
            self.outline_hint_var.set("正在读取现有书签…")
        if preview.page_count <= 0:
            self._render_generation += 1
            self._cover_image = None
            self._cover_message = preview.reason
            self._refresh_cover()
            return
        if not self._source_is_file and preview.bookmark_count and not preview.bookmarks:
            threading.Thread(target=self._read_selected_outline,
                             args=(selection_generation, preview.path), daemon=True).start()
        self._request_page(preview.path, 1)

    def _set_action_detail(self, preview: PdfPreview) -> None:
        self.outline_action_var.set(_display_reason(preview, dry_run=self.dry_run_var.get()))
        if preview.eligible and _processing_label(
                preview, replace_existing=self.replace_var.get(),
                verify_existing=self.verify_var.get()) == "替换":
            self.outline_action_label.configure(bg="#FFF4E8", fg="#975416")
        elif preview.eligible:
            self.outline_action_label.configure(bg=PALE_BLUE, fg=BLUE)
        else:
            self.outline_action_label.configure(bg=COLORS.notice, fg=MUTED)

    def _request_page(self, path: Path, page_number: int) -> None:
        self._render_generation += 1
        generation = self._render_generation
        self._cover_image = None
        self._cover_message = f"正在生成第 {page_number} 页预览…"
        self._refresh_cover()
        cache_key = (path, page_number)
        cached = self._cover_cache.get(cache_key)
        if cached is not None:
            self._cover_cache.move_to_end(cache_key)
            self._cover_image = cached
            self._refresh_cover()
            return
        threading.Thread(target=self._render_cover, args=(generation, path, page_number),
                         daemon=True).start()

    def _read_selected_outline(self, generation: int, path: Path) -> None:
        detail = inspect_pdf(path, verify_existing=True)
        self._events.put(("outline", (generation, path, detail)))

    def _render_cover(self, generation: int, path: Path, page_number: int) -> None:
        try:
            picture = (render_first_page(path, max_size=1400) if page_number == 1
                       else render_page(path, page_number, max_size=1400))
            self._events.put(("cover", (generation, path, page_number, picture, "")))
        except Exception as error:
            self._events.put(("cover", (generation, path, page_number, None, str(error))))

    def _populate_outline(self, nodes: tuple[BookmarkNode, ...]) -> None:
        self.outline_tree.delete(*self.outline_tree.get_children())
        index = 0

        def add(parent: str, items: tuple[BookmarkNode, ...], level: int) -> None:
            nonlocal index
            for node in items:
                index += 1
                title = ("  " * level) + (node.title or "（未命名）")
                item = self.outline_tree.insert(
                    parent, "end", text=str(index), values=(title, node.page_number or "—"),
                    open=level < 1, tags=("alternate",) if index % 2 == 0 else ())
                if node.children:
                    add(item, node.children, level + 1)

        add("", nodes, 0)
        self.outline_hint_var.set(
            "选中书签可跳到目标页" if nodes else "这本 PDF 目前没有书签")

    def _on_outline_selected(self, _event: object = None) -> None:
        selected = self.outline_tree.selection()
        if selected:
            page = self.outline_tree.set(selected[0], "page")
            if str(page).isdigit():
                self._go_to_page(int(page))

    def _on_result_selected(self, _event: object = None) -> None:
        selected = self.result_tree.selection()
        if selected:
            page = self.result_tree.set(selected[0], "page")
            if str(page).isdigit():
                self._go_to_page(int(page))

    def _populate_result(self, path: Path | None) -> None:
        self.result_tree.delete(*self.result_tree.get_children())
        row = self._run_results.get(str(path)) if path is not None else None
        if not row:
            self.result_count_var.set("尚无本次结果")
            self.result_detail_var.set("处理后可在此查看识别书签和复核原因")
            self.toc_button.configure(state="disabled")
            self.open_result_button.configure(state="disabled")
            return
        entries = row.get("entries") or []
        contents = row.get("toc_bookmark")
        if isinstance(contents, dict):
            # The bookmark to the contents page itself precedes the entries.
            self.result_tree.insert("", "end", text="—",
                                    values=(str(contents.get("title") or "目录"),
                                            contents.get("pdf_page") or "—"))
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                continue
            level = max(1, min(int(entry.get("level") or 1), 8))
            title = "  " * (level - 1) + str(entry.get("title") or "（未命名）")
            self.result_tree.insert("", "end", text=str(index),
                                    values=(title, entry.get("pdf_page") or "—"))
        status = self._row_action(str(path), self._previews[str(path)]) if str(path) in self._previews else str(row.get("status", ""))
        self.result_count_var.set(f"{status} · 识别 {len(entries)} 条")
        details = [str(row.get("error"))] if row.get("error") else []
        details.extend(str(item) for item in (row.get("warnings") or [])[:2])
        explanation = "；".join(details)
        if len(explanation) > 180:
            explanation = explanation[:177] + "…"
        self.result_detail_var.set(explanation or "识别结果已记录；选中书签可查看目标页")
        toc_pages = row.get("toc_pages") or []
        self.toc_button.configure(state="normal" if toc_pages else "disabled")
        output = row.get("output")
        self.open_result_button.configure(
            state="normal" if output and Path(output).is_file() else "disabled")

    def _jump_to_toc(self) -> None:
        row = self._run_results.get(str(self._displayed_path))
        pages = row.get("toc_pages") if row else None
        if pages and str(pages[0]).isdigit():
            self._go_to_page(int(pages[0]))

    def _open_result_pdf(self) -> None:
        row = self._run_results.get(str(self._displayed_path))
        output = Path(row["output"]) if row and row.get("output") else None
        if output and output.is_file():
            try:
                os.startfile(output)  # type: ignore[attr-defined]
            except OSError as exc:
                messagebox.showerror("无法打开生成的 PDF", str(exc), parent=self.root)

    def _clear_preview(self) -> None:
        self._displayed_path = None
        self._selection_generation += 1
        self._render_generation += 1
        self._page_number = 1
        self._page_count = 0
        self.page_var.set("1")
        self.page_total_var.set("/ 0")
        self._zoom_factor = 1.0
        self.zoom_var.set("100%")
        self._update_page_controls()
        self.preview_title_var.set("选择左侧文件")
        self.preview_meta_var.set("首页将在这里显示")
        self.outline_count_var.set("现有书签")
        self.outline_action_var.set("选择文件后显示处理方式")
        self.outline_action_label.configure(bg=COLORS.notice, fg=MUTED)
        self.outline_hint_var.set("选择文件后显示原有书签")
        self.outline_tree.delete(*self.outline_tree.get_children())
        self._populate_result(None)
        self._cover_image = None
        self._cover_message = "选择左侧文件查看首页"
        self._refresh_cover()

    def _schedule_cover_redraw(self, _event: object = None) -> None:
        if self._resize_after:
            self.root.after_cancel(self._resize_after)
        self._resize_after = self.root.after(100, self._refresh_cover)

    def _update_page_controls(self) -> None:
        available = self._displayed_path is not None and self._page_count > 0
        self.page_var.set(str(self._page_number))
        self.page_total_var.set(f"/ {self._page_count}")
        self.prev_button.configure(state="normal" if available and self._page_number > 1 else "disabled")
        self.next_button.configure(state="normal" if available and self._page_number < self._page_count else "disabled")
        self.page_entry.configure(state="normal" if available else "disabled")
        self.fullscreen_button.configure(state="normal" if available else "disabled")
        self.zoom_out_button.configure(state="normal" if available and self._zoom_factor > 0.5 else "disabled")
        self.zoom_in_button.configure(state="normal" if available and self._zoom_factor < 2.5 else "disabled")
        self.zoom_box.configure(state="readonly" if available else "disabled")
        if self._fullscreen_window is not None and self._fullscreen_window.winfo_exists():
            self.fullscreen_page_var.set(f"第 {self._page_number} / {self._page_count} 页")

    def _go_to_page(self, page_number: int) -> None:
        if self._displayed_path is None or self._page_count < 1:
            return
        page_number = min(max(page_number, 1), self._page_count)
        if page_number != self._page_number:
            self._page_number = page_number
            self._update_page_controls()
            self._request_page(self._displayed_path, page_number)
        else:
            self.page_var.set(str(self._page_number))

    def _step_page(self, change: int) -> None:
        self._go_to_page(self._page_number + change)

    def _on_page_entered(self, _event: object = None) -> None:
        try:
            page_number = int(self.page_var.get().strip())
        except ValueError:
            self.page_var.set(str(self._page_number))
            return
        self._go_to_page(page_number)

    def _set_zoom(self, percent: int) -> None:
        percent = min(max(percent, 50), 250)
        self._zoom_factor = percent / 100
        self.zoom_var.set(f"{percent}%")
        self._update_page_controls()
        self._refresh_cover()

    def _step_zoom(self, change: int) -> None:
        self._set_zoom(round(self._zoom_factor * 100) + change)

    def _on_zoom_selected(self, _event: object = None) -> None:
        try:
            self._set_zoom(int(self.zoom_var.get().rstrip("%")))
        except ValueError:
            self.zoom_var.set(f"{round(self._zoom_factor * 100)}%")

    def _open_fullscreen(self) -> None:
        if self._displayed_path is None or self._page_count < 1:
            return
        if self._fullscreen_window is not None and self._fullscreen_window.winfo_exists():
            self._fullscreen_window.lift()
            return
        window = tk.Toplevel(self.root)
        self._fullscreen_window = window
        window.title(f"PDF 预览 · {self._displayed_path.name}")
        window.configure(bg=BG)
        window.attributes("-fullscreen", True)
        window.protocol("WM_DELETE_WINDOW", self._close_fullscreen)
        window.bind("<Escape>", lambda _event: self._close_fullscreen())
        window.bind("<Left>", lambda _event: self._step_page(-1))
        window.bind("<Right>", lambda _event: self._step_page(1))
        bar = tk.Frame(window, bg=WHITE, padx=18, pady=8)
        bar.pack(fill="x")
        self.fullscreen_page_var = tk.StringVar(value=f"第 {self._page_number} / {self._page_count} 页")
        self._label(bar, "PDF 页面预览", bold=True, size=12).pack(side="left", padx=(0, 20))
        ttk.Button(bar, text="‹ 上一页", command=lambda: self._step_page(-1),
                   style="App.TButton").pack(side="left")
        self._label(bar, textvariable=self.fullscreen_page_var).pack(side="left", padx=16)
        ttk.Button(bar, text="下一页 ›", command=lambda: self._step_page(1),
                   style="App.TButton").pack(side="left")
        ttk.Button(bar, text="退出全屏 Esc", command=self._close_fullscreen,
                   style="App.TButton").pack(side="right")
        ttk.Button(bar, text="放大 +", command=lambda: self._step_zoom(25),
                   style="App.TButton").pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="缩小 −", command=lambda: self._step_zoom(-25),
                   style="App.TButton").pack(side="right")
        canvas = tk.Canvas(window, bg=COLORS.preview, highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=14, pady=14)
        canvas.bind("<Configure>", self._schedule_cover_redraw)
        canvas.bind("<ButtonPress-1>", lambda event: canvas.scan_mark(event.x, event.y))
        canvas.bind("<B1-Motion>", lambda event: canvas.scan_dragto(event.x, event.y, gain=1))
        self._fullscreen_canvas = canvas
        self._refresh_cover()

    def _close_fullscreen(self) -> None:
        window = self._fullscreen_window
        self._fullscreen_window = None
        self._fullscreen_canvas = None
        self._fullscreen_photo = None
        if window is not None and window.winfo_exists():
            window.destroy()

    def _draw_cover_on(self, canvas: tk.Canvas) -> ImageTk.PhotoImage | None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), 20)
        height = max(canvas.winfo_height(), 20)
        if self._cover_image is None:
            canvas.configure(scrollregion=(0, 0, width, height))
            canvas.create_text(width // 2, height // 2, text=self._cover_message,
                               fill=MUTED, font=font(10), width=max(width - 35, 20),
                               justify="center")
            return None
        image = self._cover_image.copy()
        image.thumbnail((max(width - 28, 20), max(height - 28, 20)), Image.Resampling.LANCZOS)
        image = image.resize((max(1, round(image.width * self._zoom_factor)),
                              max(1, round(image.height * self._zoom_factor))),
                             Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image, master=canvas)
        region_width = max(width, image.width + 28)
        region_height = max(height, image.height + 28)
        x = region_width // 2
        y = region_height // 2
        canvas.configure(scrollregion=(0, 0, region_width, region_height))
        canvas.create_rectangle(x - image.width // 2 - 2, y - image.height // 2 - 2,
                                x + image.width // 2 + 2, y + image.height // 2 + 2,
                                fill=WHITE, outline=BORDER)
        canvas.create_image(x, y, image=photo)
        return photo

    def _refresh_cover(self) -> None:
        self._resize_after = None
        self._cover_photo = self._draw_cover_on(self.cover_canvas)
        if self._fullscreen_canvas is not None and self._fullscreen_canvas.winfo_exists():
            self._fullscreen_photo = self._draw_cover_on(self._fullscreen_canvas)

    def _update_start_state(self) -> None:
        ready = any(preview.eligible for preview in self._previews.values())
        ready = ready and not self._scanning and not self._running
        ready = ready and not (self.resume_var.get() and (
            self._resume_load_pending or self._resume_load_error))
        if self._source_is_file:
            label = "仅分析这本 PDF" if self.dry_run_var.get() else "处理这本 PDF"
        else:
            action = "仅分析" if self.dry_run_var.get() else "处理"
            label = f"{action}整批 {len(self._previews)} 本"
        self.start_button.configure(state="normal" if ready else "disabled", text=label)

    def _set_running(self, running: bool) -> None:
        self._running = running
        entry_state = "disabled" if running else "normal"
        for widget in (self.input_entry, self.input_button, self.file_button,
                       self.clear_input_button, self.dry_checkbox, self.resume_checkbox,
                       self.overwrite_original_checkbox):
            widget.configure(state=entry_state)
        self._update_output_controls()
        for button in self.policy_buttons:
            button.configure(state=entry_state)
        self.compare_on_replace_checkbox.configure(
            state="disabled" if running or self.policy_var.get() != "replace" else "normal")
        self._update_api_key_controls()
        self._update_start_state()
        self.stop_button.configure(state="normal" if running else "disabled")

    def _append(self, message: str, *, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _append_progress(self, index: int, total: int, filename: str,
                         *, resumed: bool, source: str | None = None) -> None:
        """Give each book an easy-to-find heading in the copyable log."""
        self.log.configure(state="normal")
        if self.log.compare("end-1c", ">", "1.0"):
            self.log.insert("end", "\n")
        heading_start = self.log.index("end-1c")
        self.log.insert("end", f"第 {index}/{total} 本\n", "book_progress")
        if source is not None:
            self._progress_heading_ranges.setdefault(source, []).append(
                (heading_start, self.log.index("end-1c")))
        action = ("续跑记录显示已完成，本次跳过。" if resumed else "正在处理。")
        self.log.insert("end", f"{filename}；{action}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_failed_progress(self, source: str, failed: bool) -> None:
        self.log.configure(state="normal")
        for start, end in self._progress_heading_ranges.get(source, ()):
            if failed:
                self.log.tag_add("failed_progress", start, end)
            else:
                self.log.tag_remove("failed_progress", start, end)
        self.log.tag_raise("failed_progress", "book_progress")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._progress_heading_ranges.clear()

    def _result_log_message(self, row: dict) -> str:
        source = str(row.get("source") or "")
        preview = self._previews.get(source)
        name = self._file_label(preview) if preview is not None else Path(source).name
        entries = row.get("entries")
        count = len(entries) if isinstance(entries, list) else 0
        status = row.get("status")
        if status == "success":
            written = count + (1 if isinstance(row.get("toc_bookmark"), dict) else 0)
            detail = f"已写入 {written} 条书签"
        elif status == "skipped":
            detail = "已跳过，原 PDF 未改动"
            if count:
                detail += f"；识别到 {count} 条目录条目"
        elif status == "dry_run":
            detail = f"分析完成，识别到 {count} 条目录条目，未修改 PDF"
        elif status == "needs_review":
            detail = f"需要人工复核，识别到 {count} 条目录条目，未修改 PDF"
        elif status == "failed":
            detail = "处理失败，原 PDF 未改动"
        else:
            detail = "处理结果已记录"
        pages = _directory_pages_text(row.get("toc_pages"))
        if pages:
            detail += f"；印刷目录位于 {pages}"
        warnings = row.get("warnings")
        first_warning = warnings[0] if isinstance(warnings, list) and warnings else ""
        issue = _friendly_issue(row.get("error") or first_warning)
        if issue:
            detail += f"；{issue}"
        return f"{name}：{detail}。"

    def _toggle_log(self) -> None:
        self._set_log_visible(self.detail_tabs.select() != str(self.log_tab))

    def _set_log_visible(self, visible: bool) -> None:
        self.detail_tabs.select(self.log_tab if visible else 0)

    def _on_detail_tab_changed(self, _event: object = None) -> None:
        self._log_visible = self.detail_tabs.select() == str(self.log_tab)
        if hasattr(self, "log_button"):
            self.log_button.configure(text="查看书签" if self._log_visible else "查看日志",
                                      icon_name="bookmark" if self._log_visible else "file-text")

    def _start(self) -> None:
        if self._running or self._scanning:
            return
        signature = self._current_signature()
        if signature != self._loaded_signature:
            self._load_source()
            return
        input_text = self.input_var.get().strip()
        output_text = self.output_var.get().strip()
        overwrite = self.overwrite_original_var.get()
        if not input_text or (not overwrite and not output_text):
            prompt = ("请选择 PDF 文件或文件夹。" if overwrite else
                      "请选择 PDF 文件或文件夹，以及输出文件夹。")
            messagebox.showerror("路径未选择", prompt, parent=self.root)
            return
        input_path = Path(input_text).expanduser().resolve()
        output_dir = self._resolved_output_dir()
        single_file = input_path.is_file()
        if single_file and input_path.suffix.lower() != ".pdf":
            messagebox.showerror("输入文件无效", f"请选择 PDF 文件：\n{input_path}", parent=self.root)
            return
        if not single_file and not input_path.is_dir():
            messagebox.showerror("输入路径无效", f"找不到 PDF 文件或文件夹：\n{input_path}", parent=self.root)
            return
        if not overwrite and output_dir.exists() and not output_dir.is_dir():
            messagebox.showerror("输出路径无效", f"请选择输出文件夹：\n{output_dir}", parent=self.root)
            return
        if not overwrite and not single_file and (input_path.is_relative_to(output_dir)
                                                  or output_dir.is_relative_to(input_path)):
            messagebox.showerror("输出文件夹无效", "输入和输出文件夹不能相同，也不能互相包含。",
                                 parent=self.root)
            return
        try:
            current_paths = ([input_path] if single_file
                             else list_pdf_paths(input_path, None if overwrite else output_dir))
        except OSError as exc:
            messagebox.showerror("无法检查 PDF 清单", str(exc), parent=self.root)
            return
        if ([str(path) for path in current_paths] != list(self._previews)
                or any(_file_fingerprint(path) != self._scan_fingerprints.get(str(path))
                       for path in current_paths)):
            self._load_source(force=True, source_changed=True)
            return
        if not any(preview.eligible for preview in self._previews.values()):
            messagebox.showinfo("没有待处理 PDF", "当前规则下没有需要识别的 PDF。", parent=self.root)
            return
        if single_file and not next(iter(self._previews.values())).eligible:
            return
        api_key = (self.api_key_var.get().strip() or self._cached_api_key
                   or os.environ.get("DEEPSEEK_API_KEY", "").strip())
        if not api_key:
            messagebox.showerror("缺少 DeepSeek API Key",
                                 "请输入 DeepSeek API Key，或设置 DEEPSEEK_API_KEY 环境变量。",
                                 parent=self.root)
            return

        self._persist_api_key()

        environment = build_batch_environment(api_key=api_key)
        job_dir = job_data_dir(input_path, output_dir, overwrite)
        try:
            migrate_legacy_job_data(output_dir, job_dir, overwrite_original=overwrite)
        except LegacyDataMigrationError:
            messagebox.showerror("无法整理输出目录",
                                 "无法迁移旧版程序留下的报告或缓存。请检查文件是否被占用，"
                                 "或更换一个空的输出目录。", parent=self.root)
            return
        self._clear_run_results()
        self._report_path = job_dir / REPORT_NAME
        self._report_before = _file_fingerprint(self._report_path)
        self._stop_requested = False
        self._stop_file = job_dir / f"stop-{uuid.uuid4().hex}.flag"
        self._worker_total = len(current_paths)
        self._worker_seen_paths.clear()
        try:
            self._run_report = RunReport(job_dir / "bookmarker-report.jsonl")
            command = build_batch_command(
                input_path, output_dir, single_file=single_file,
                dry_run=self.dry_run_var.get(), replace_existing=self.replace_var.get(),
                verify_existing=self.verify_var.get(),
                skip_bookmarked=self.skip_bookmarked_var.get(),
                overwrite_original=self.overwrite_original_var.get(),
                resume=self.resume_var.get(), stop_file=self._stop_file)
            self._process = subprocess.Popen(
                command, cwd=self.working_dir, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self._run_report = None
            self._stop_file = None
            messagebox.showerror("启动失败", str(exc), parent=self.root)
            return
        self._run_status = {path: "queued" for path, preview in self._previews.items()
                            if preview.eligible}
        self._clear_log()
        self._rebuild_list()
        self.report_var.set("报告保存在软件目录的 data 文件夹")
        self.report_button.configure(state="disabled")
        self.status_var.set("处理中…")
        scope = f"《{input_path.name}》" if single_file else f"文件夹《{input_path.name}》"
        destination = ("仅分析，不修改 PDF" if self.dry_run_var.get() else
                       "成功后覆盖原 PDF" if self.overwrite_original_var.get() else
                       "新 PDF 保存到所选输出目录")
        self._append(f"开始处理 {scope}；{destination}。")
        self._set_log_visible(True)
        self._set_running(True)
        threading.Thread(target=self._read_process, args=(self._process,), daemon=True).start()

    def _read_process(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                self._events.put(("line", line.rstrip("\r\n")))
        finally:
            process.stdout.close()
            self._events.put(("done", process.wait()))

    def _handle_worker_line(self, line: str) -> None:
        found = (re.match(r"^找到 (\d+) 本 PDF：", line) or
                 re.match(r"^Found (\d+) PDF files in ", line))
        if found:
            self._worker_total = int(found.group(1))
            self._append(f"找到 {self._worker_total} 本 PDF，开始逐本检查。")
            return
        if line.startswith(("正在处理 PDF：", "Processing PDF file: ")):
            self._append("已找到所选 PDF，准备检查。")
            return
        match = re.match(r"^第 (\d+)/(\d+) 本：(.*?)(；续跑已完成，本次跳过|；正在处理)$", line)
        chinese_line = match is not None
        if match is None:
            match = re.match(r"^\[(\d+)/(\d+)\]\s*(.*)", line)
        if match:
            index = int(match.group(1))
            self._worker_total = int(match.group(2))
            detail = match.group(3)
            resumed = (match.group(4) == "；续跑已完成，本次跳过" if chinese_line else
                       detail.startswith("resume skip: "))
            relative = (detail if chinese_line else
                        detail[len("resume skip: "):] if resumed else detail)
            if self._source_path is not None:
                source_dir = (self._source_path.parent if self._source_is_file
                              else self._source_path)
                path = str((source_dir / relative).resolve())
                self._worker_seen_paths.add(path)
                self._file_indices[path] = index
                if path in self._previews and self._run_status.get(path) in {
                    None, "queued", "running"
                }:
                    self._run_status[path] = "resume_skip" if resumed else "running"
                    self._rebuild_list()
            self.status_var.set(f"正在检查第 {index}/{match.group(2)} 本")
            self._append_progress(index, self._worker_total, relative,
                                  resumed=resumed, source=path if self._source_path else None)
            return
        if (re.match(r"^\s+(success|skipped|dry_run|needs_review|failed):", line)
                or line.startswith(("Summary:", "Summary CSV:", "Detailed report:",
                                    "处理结果：", "本次汇总：", "本次已统计", "本次未获取",
                                    "汇总报告：", "详细报告："))):
            return
        if line.startswith("无法在软件目录保存运行数据"):
            self._append(line)
            return
        if line.startswith("已将旧版报告和缓存移到软件目录"):
            self._append(line)
            return
        if line.startswith("无法整理输出目录中旧版程序留下的报告或缓存"):
            self._append(line)
            return
        if line.startswith("已收到停止请求"):
            self._append("当前 PDF 已处理完，后续文件不会开始。")
            return
        # stdout and stderr are merged. PDF libraries can emit warnings while
        # processing a book; the structured result row explains its outcome.
        # Reporting an unknown line here would imply failure before that row
        # arrives, even when processing continues normally.

    def _drain_run_report(self) -> None:
        if self._run_report is None:
            return
        try:
            rows = self._run_report.read_new()
        except OSError:
            self._append("无法读取本次结果，请检查软件目录是否可写。")
            return
        for row in rows:
            source = row["source"]
            signature = repr(row)
            if self._logged_results.get(source) != signature:
                failed = row.get("status") == "failed"
                self._set_failed_progress(source, failed)
                self._append(self._result_log_message(row),
                             tag="failed_result" if failed else None)
                self._logged_results[source] = signature
        records = self._run_report.records
        if records == self._run_results:
            return
        removed = self._run_results.keys() - records.keys()
        for path in removed:
            self._resume_done.pop(path, None)
        self._run_results = records
        self._run_status = {
            path: status for path, status in self._run_status.items()
            if status in {"queued", "running", "resume_skip"}
        }
        for path, row in records.items():
            self._run_status[path] = row["status"]
            self._resume_done[path] = row
        self._rebuild_list()
        if self._displayed_path is not None:
            self._populate_result(self._displayed_path)
        self._update_run_summary()
        self._update_token_usage()

    def _update_token_usage(self, *, completed: bool = False) -> None:
        all_skipped = (completed and self._worker_total > 0 and
                       sum(status == "resume_skip" for status in
                           self._run_status.values()) == self._worker_total)
        message = _token_usage_text(self._run_results, completed=completed,
                                    all_skipped=all_skipped)
        self.token_usage_var.set(message)
        if message:
            self.plan_bar.grid_configure(pady=(0, 6))
            self.token_usage_label.grid()
        else:
            self.token_usage_label.grid_remove()
            self.plan_bar.grid_configure(pady=(0, 14))

    def _update_run_summary(self, *, finished: bool = False, exit_code: int = 0) -> None:
        counts = self._run_report.status_counts if self._run_report else {}
        resumed = sum(status == "resume_skip" for status in self._run_status.values())
        completed = sum(counts.values()) + resumed
        total = max(self._worker_total or len(self._previews), completed)
        parts = [f"已检查 {completed}/{total}", f"成功 {counts.get('success', 0)}"]
        if self.dry_run_var.get():
            parts.append(f"已分析 {counts.get('dry_run', 0)}")
        parts.extend((f"跳过 {counts.get('skipped', 0) + resumed}",
                      f"需复核 {counts.get('needs_review', 0)}",
                      f"失败 {counts.get('failed', 0)}"))
        summary = "  ·  ".join(parts)
        self.run_summary_var.set(summary)
        if finished:
            unfinished = "interrupted" if self._stop_requested else "unprocessed"
            for path, status in tuple(self._run_status.items()):
                if status in {"queued", "running"}:
                    self._run_status[path] = unfinished
            self._rebuild_list()
            if self._stop_requested:
                self.status_var.set("已停止；已完成部分见结果和报告")
            elif counts.get("failed", 0):
                self.status_var.set(f"处理结束；{counts['failed']} 本失败")
            elif exit_code != 0:
                self.status_var.set("处理未完成；请查看日志")
            elif counts.get("needs_review", 0):
                self.status_var.set(f"处理结束；{counts['needs_review']} 本需复核")
            elif resumed and not counts:
                self.status_var.set(f"已按续跑设置跳过 {resumed} 本")
            else:
                self.status_var.set("处理完成")
            if self._stop_requested or (exit_code != 0 and not counts.get("failed", 0)):
                self.filter_var.set("全部")
            elif counts.get("failed", 0):
                self.filter_var.set("失败")
            elif counts.get("needs_review", 0):
                self.filter_var.set("需复核")
            else:
                self.filter_var.set("全部")
            if counts:
                self.detail_tabs.select(1)

    def _poll_events(self) -> None:
        self._poll_after = None
        if self._closed:
            return
        for _ in range(100):
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "scan_total":
                generation, total, done, error = value
                if generation == self._scan_generation:
                    self._scan_total = total
                    if done is not None or error:
                        self._resume_load_pending = False
                        self._resume_load_error = error
                    if done is not None:
                        self._resume_done = done
                        self._resume_done_loaded = True
                    if error:
                        self._append(f"读取续跑记录失败：{error}")
                    self.count_var.set(f"正在检查 0/{total} 本 PDF…")
                    ordinal_width = max(62, 39 + len(str(max(total, 1))) * 8)
                    self.pdf_tree.column("#0", width=ordinal_width, minwidth=ordinal_width)
            elif kind == "resume_done":
                generation, done, error = value
                if generation == self._scan_generation:
                    self._resume_load_pending = False
                    if error:
                        self._resume_load_error = error
                        self._append(f"读取续跑记录失败：{error}")
                        self.status_var.set("续跑记录无法读取；请检查报告文件")
                    else:
                        self._resume_load_error = ""
                        self._resume_done = done
                        self._resume_done_loaded = True
                        if self.resume_var.get():
                            self._refresh_plan()
                    self._update_plan_summary()
            elif kind == "scan_item":
                generation, index, preview, fingerprint = value
                self._handle_scan_item(generation, index, preview, fingerprint)
            elif kind == "scan_done":
                generation = value
                if generation == self._scan_generation:
                    self._scanning = False
                    self._rebuild_list()
                    self._update_plan_summary()
                    self.list_hint_var.set(
                        "单击预览，双击用本机 PDF 阅读器打开；运行会检查全部文件"
                        if not self._source_is_file else "双击可用本机 PDF 阅读器打开原文件")
                    if self._source_is_file and self._previews:
                        only = next(iter(self._previews.values()))
                        self.status_var.set(
                            f"预计{_processing_label(only, replace_existing=self.replace_var.get(), verify_existing=self.verify_var.get())}"
                            if only.eligible else only.reason)
                    else:
                        ready = sum(item.eligible for item in self._previews.values())
                        self.status_var.set(f"扫描完成；预计识别 {ready} 本")
                    if self.resume_var.get() and self._resume_load_error:
                        self.status_var.set("续跑记录无法读取；请关闭续跑或修复报告")
                    if self._source_changed_before_run:
                        self.status_var.set("来源文件已变化；请检查计划后再次开始")
                        self._source_changed_before_run = False
                    if self._post_run_message is not None:
                        status, summary = self._post_run_message
                        self.status_var.set(status)
                        self.run_summary_var.set(summary)
                        self._post_run_message = None
                    self._update_start_state()
            elif kind == "scan_error":
                generation, detail = value
                if generation == self._scan_generation:
                    self._scanning = False
                    self._resume_load_pending = False
                    self.count_var.set("扫描失败")
                    self.list_hint_var.set(detail)
                    if self._post_run_message is not None:
                        status, summary = self._post_run_message
                        self.status_var.set(f"{status}；刷新预览失败")
                        self.run_summary_var.set(summary)
                        self._post_run_message = None
                    else:
                        self.status_var.set("无法读取来源")
                        self.run_summary_var.set("扫描失败；请检查来源和输出位置")
                    self._update_start_state()
            elif kind == "cover":
                generation, path, page_number, picture, detail = value
                if generation == self._render_generation:
                    if picture is not None:
                        self._cover_image = picture
                        cache_key = (path, page_number)
                        self._cover_cache[cache_key] = picture
                        self._cover_cache.move_to_end(cache_key)
                        while len(self._cover_cache) > 8:
                            self._cover_cache.popitem(last=False)
                    else:
                        self._cover_image = None
                        self._cover_message = detail or "无法预览首页"
                    self._refresh_cover()
            elif kind == "outline":
                generation, path, detail = value
                if generation == self._selection_generation and detail.path == path:
                    self._base_previews[str(path)] = detail
                    self._previews[str(path)] = self._classify_preview(detail)
                    self._populate_outline(detail.bookmarks)
            elif kind == "line":
                self._handle_worker_line(str(value))
                self._drain_run_report()
            elif kind == "done":
                exit_code = int(value)
                self._drain_run_report()
                self._process = None
                if self._stop_file is not None:
                    try:
                        self._stop_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._stop_file = None
                if self._stop_requested and exit_code == 0:
                    self._stop_requested = False
                self._set_running(False)
                self._update_run_summary(finished=True, exit_code=exit_code)
                self._update_token_usage(completed=True)
                if exit_code != 0 and not self._stop_requested and not self._run_results:
                    self._append("处理程序提前结束，未取得文件结果；请检查来源、API Key 和软件目录权限。")
                self._append("本次处理汇总：" + self.run_summary_var.get().replace("  ·  ", "，") + "。")
                if self.token_usage_var.get():
                    self._append(self.token_usage_var.get() + "。")
                if (self._report_path
                        and _file_fingerprint(self._report_path) is not None
                        and _file_fingerprint(self._report_path) != self._report_before):
                    self.report_button.configure(state="normal")
                    self._append("详细报告已保存到软件目录，可点击“打开报告”查看。")
                else:
                    self.report_var.set("本次没有新增报告；续跑跳过的书可查看已有报告")
                    self._append("本次没有新增报告。")
                if self._close_when_done:
                    self._finish_close()
                    return
                if ((self.overwrite_original_var.get() and not self.dry_run_var.get())
                        or self._worker_total != len(self._previews)
                        or (self._worker_seen_paths
                            and self._worker_seen_paths != set(self._previews))
                        or bool(self._run_results.keys() - self._previews.keys())):
                    self._load_source(force=True, keep_results=True)
        if self._running:
            self._drain_run_report()
        self._poll_after = self.root.after(100, self._poll_events)

    def _stop(self) -> bool:
        if self._stop_requested or not self._running:
            return True
        if self._process is None or self._process.poll() is not None:
            return True
        if self._stop_file is None:
            messagebox.showerror("无法停止", "没有找到本次处理的停止标记位置。", parent=self.root)
            return False
        try:
            self._stop_file.parent.mkdir(parents=True, exist_ok=True)
            self._stop_file.write_text("stop\n", encoding="utf-8")
        except OSError as error:
            messagebox.showerror("无法停止", f"无法写入停止标记：{error}", parent=self.root)
            return False
        self._stop_requested = True
        self.status_var.set("已请求停止；正在等待当前 PDF 处理完成…")
        self._append("已请求停止；当前 PDF 处理完后，不再开始下一本。")
        self.stop_button.configure(state="disabled")
        return True

    def _open_report(self) -> None:
        if self._report_path and self._report_path.is_file():
            try:
                os.startfile(self._report_path)  # type: ignore[attr-defined]
            except OSError as exc:
                messagebox.showerror("无法打开报告", str(exc), parent=self.root)

    def _close(self) -> None:
        if self._closed:
            return
        if self._running:
            if not self._stop_requested:
                if not messagebox.askyesno(
                        "处理尚未结束", "当前 PDF 处理完后停止，并关闭窗口？", parent=self.root):
                    return
                if not self._stop():
                    return
            self._close_when_done = True
            self.status_var.set("正在完成当前 PDF；结束后关闭窗口…")
            return
        self._finish_close()

    def _finish_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._persist_api_key()
        self._scan_cancel.set()
        self._close_fullscreen()
        for after_id in (self._poll_after, self._load_after, self._resize_after):
            if after_id:
                self.root.after_cancel(after_id)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    root.withdraw()
    try:
        restored = recover_pending_overwrites()
    except Exception as error:
        messagebox.showerror(
            "需要处理上次中断的 PDF",
            f"覆盖恢复检查未通过：{error}\n\n为保护原 PDF，软件暂不开始新任务。",
            parent=root)
        root.destroy()
        return
    app = BookmarkApp(root)
    if restored:
        app.status_var.set(f"已恢复上次中断的 {len(restored)} 本原 PDF")
        app._append(f"已恢复上次中断的 {len(restored)} 本原 PDF：")
        for path in restored:
            app._append(f"已恢复：{path}")
        app._set_log_visible(True)
    root.deiconify()
    if restored:
        messagebox.showinfo(
            "已恢复中断的 PDF",
            f"已从软件目录的备份恢复 {len(restored)} 本原 PDF。具体文件见日志。",
            parent=root)
    root.mainloop()


if __name__ == "__main__":
    main()
