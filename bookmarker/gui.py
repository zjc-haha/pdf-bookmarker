"""Desktop workspace for previewing PDFs and adding bookmarks."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from .__main__ import _done_files
from .key_cache import KeyCacheError, load_key, save_key
from .pipeline import _outline_action
from .toc import HIERARCHY_VERSION
from .preview import (BookmarkNode, PdfPreview, inspect_pdf, list_pdf_paths,
                      render_first_page, render_page)
from .ui_style import COLORS, RoundedButton, apply_theme, icon


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


def default_directories(*, frozen: bool | None = None) -> tuple[Path, Path, Path]:
    """Return input, output, and working directories for this installation."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        for location in (PROJECT_ROOT.parent, PROJECT_ROOT):
            source = location / "books"
            if source.is_dir():
                return source, location / "bookmarked", PROJECT_ROOT
        return PROJECT_ROOT, PROJECT_ROOT / "bookmarked", PROJECT_ROOT

    home = Path.home()
    documents = home / "Documents"
    if documents.is_dir():
        source = documents
    else:
        source = home / "PDF书籍"
        source.mkdir(parents=True, exist_ok=True)
    return source, home / "PDF书签", source


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


def _processing_label(preview: PdfPreview, *, replace_existing: bool = False,
                      verify_existing: bool = False, dry_run: bool = False) -> str:
    """The candidate-list action, using the same outline rule as processing."""
    if not preview.eligible:
        return "跳过"
    if dry_run:
        return "分析"
    if not preview.bookmark_count:
        return "新增"
    if verify_existing and not replace_existing and preview.outline_quality >= 0.55:
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
        self.root.geometry("1500x850")
        self.root.minsize(1120, 700)
        self.root.configure(bg=BG)
        self._configure_style()
        self._icons: list[ImageTk.PhotoImage] = []

        default_input, default_output, self.working_dir = default_directories()
        self.input_var = tk.StringVar(value=str(default_input))
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
        self.overwrite_original_var = tk.BooleanVar(value=False)
        self.resume_var = tk.BooleanVar(value=False)
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.report_var = tk.StringVar(value="处理后可查看报告")
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

        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._process: subprocess.Popen[str] | None = None
        self._report_path: Path | None = None
        self._report_before: tuple[int, int] | None = None
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
        self._file_indices: dict[str, int] = {}
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
        self._scan_skipped = 0
        self._scan_failed = 0
        self._scan_resumed = 0

        self._build_widgets()
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
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._poll_after = self.root.after(100, self._poll_events)
        self._load_after = self.root.after(40, self._load_source)

    def _configure_style(self) -> None:
        apply_theme(self.root)

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
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kwargs)

    def _build_widgets(self) -> None:
        outer = tk.Frame(self.root, bg=BG, padx=26, pady=13)
        outer.pack(fill="both", expand=True)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(2, weight=1)

        header = tk.Frame(outer, bg=BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 13))
        self._badge(header, "bookmark", size=25, background=BLUE, color=WHITE).pack(
            side="left", padx=(7, 16))
        tk.Label(header, text="PDF 书签工作台", bg=BG, fg=TEXT,
                 font=("Segoe UI", 21, "bold")).pack(side="left")
        tk.Label(header, text="│  选书 · 预览 · 添加书签", bg=BG, fg=MUTED,
                 font=("Segoe UI", 10)).pack(side="left", padx=(14, 0), pady=(5, 0))
        tk.Label(header, text="DEEPSEEK", bg=PALE_BLUE, fg=BLUE,
                 font=("Segoe UI", 9, "bold"), padx=13, pady=7).pack(side="right")
        self.settings_button = RoundedButton(header, text="设置", icon_name="settings",
                                              kind="quiet", width=85, height=34,
                                              command=lambda: self.api_key_entry.focus_set())
        self.settings_button.pack(side="right", padx=(0, 15))

        source = self._card(outer)
        source.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        source.grid_columnconfigure(1, weight=1)
        label = tk.Frame(source, bg=WHITE)
        label.grid(row=0, column=0, sticky="w", padx=(15, 16), pady=(12, 7))
        self._badge(label, "pdf").pack(side="left", padx=(0, 12))
        self._label(label, "来源", bold=True).pack(side="left")
        source_path = tk.Frame(source, bg=WHITE)
        source_path.grid(row=0, column=1, sticky="ew", pady=(12, 7))
        source_path.grid_columnconfigure(0, weight=1)
        self.input_entry = ttk.Entry(source_path, textvariable=self.input_var, style="App.TEntry")
        self.input_entry.grid(row=0, column=0, sticky="ew")
        self.clear_input_button = ttk.Button(source_path, text="×", width=2, style="App.TButton",
                                              command=self._clear_input)
        self.clear_input_button.grid(row=0, column=1, padx=(4, 0))
        self.input_entry.bind("<Return>", self._on_path_committed)
        self.input_entry.bind("<FocusOut>", self._on_path_committed)
        picks = tk.Frame(source, bg=WHITE)
        picks.grid(row=0, column=2, sticky="e", padx=(10, 16), pady=(12, 7))
        self.file_button = RoundedButton(picks, text="选择 PDF", icon_name="folder",
                                          kind="secondary", width=122, height=36,
                                          command=self._pick_file)
        self.file_button.pack(side="left")
        self.input_button = RoundedButton(picks, text="选择文件夹", icon_name="folder",
                                           kind="secondary", width=134, height=36,
                                           command=self._pick_input)
        self.input_button.pack(side="left", padx=(7, 0))

        label = tk.Frame(source, bg=WHITE)
        label.grid(row=1, column=0, sticky="w", padx=(15, 16), pady=(0, 12))
        self._badge(label, "folder").pack(side="left", padx=(0, 12))
        self.output_label = self._label(label, "输出", bold=True)
        self.output_label.pack(side="left")
        output_path = tk.Frame(source, bg=WHITE)
        output_path.grid(row=1, column=1, sticky="ew", pady=(0, 12))
        output_path.grid_columnconfigure(0, weight=1)
        self.output_entry = ttk.Entry(output_path, textvariable=self.output_var, style="App.TEntry")
        self.output_entry.grid(row=0, column=0, sticky="ew")
        self.clear_output_button = ttk.Button(output_path, text="×", width=2, style="App.TButton",
                                               command=self._clear_output)
        self.clear_output_button.grid(row=0, column=1, padx=(4, 0))
        self.output_entry.bind("<Return>", self._on_path_committed)
        self.output_entry.bind("<FocusOut>", self._on_path_committed)
        self.output_button = RoundedButton(source, text="浏览输出位置", icon_name="folder",
                                            kind="secondary", width=172, height=36,
                                            command=self._pick_output)
        self.output_button.grid(row=1, column=2, sticky="e", padx=(10, 16), pady=(0, 12))

        workspace = tk.Frame(outer, bg=BG)
        workspace.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
        workspace.grid_rowconfigure(0, weight=1)
        workspace.grid_columnconfigure(0, weight=34, uniform="workspace")
        workspace.grid_columnconfigure(1, weight=36, uniform="workspace")
        workspace.grid_columnconfigure(2, weight=30, uniform="workspace")
        self._build_pdf_list(workspace)
        self._build_cover(workspace)
        self._build_outline(workspace)

        settings = self._card(outer)
        settings.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self._build_settings(settings)

        footer = tk.Frame(outer, bg=BG)
        footer.grid(row=4, column=0, sticky="ew")
        self.start_button = RoundedButton(footer, text="为此 PDF 添加书签", icon_name="play",
                                          kind="primary", width=220, height=42,
                                          command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = RoundedButton(footer, text="停止", icon_name="stop",
                                         kind="secondary", width=126, height=42,
                                         command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        tk.Label(footer, image=self._icon("info", size=17), bg=BG).pack(side="left", padx=(16, 5))
        tk.Label(footer, textvariable=self.status_var, bg=BG, fg=BLUE,
                 font=("Segoe UI", 10)).pack(side="left")
        self.report_button = RoundedButton(footer, text="打开报告", icon_name="folder",
                                           kind="secondary", width=132, height=42,
                                           command=self._open_report, state="disabled")
        self.report_button.pack(side="right")
        self.log_button = RoundedButton(footer, text="处理日志  ▾", icon_name="list",
                                        kind="secondary", width=142, height=42,
                                        command=self._toggle_log)
        self.log_button.pack(side="right", padx=(0, 8))
        tk.Label(outer, textvariable=self.report_var, bg=BG, fg=MUTED,
                 font=("Segoe UI", 9), anchor="w").grid(row=5, column=0, sticky="ew", pady=(7, 0))
        self.log_frame = self._card(outer)
        self.log = tk.Text(self.log_frame, height=5, wrap="word", state="disabled",
                           relief="flat", bg="#F9FBFE", fg=TEXT, font=("Consolas", 9),
                           padx=10, pady=7)
        self.log.pack(fill="both", expand=True)

    def _build_pdf_list(self, parent: tk.Misc) -> None:
        card = self._card(parent)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(2, weight=1)
        heading = tk.Frame(card, bg=WHITE)
        heading.grid(row=0, column=0, sticky="ew", padx=15, pady=(11, 10))
        self._badge(heading, "pdf").pack(side="left", padx=(0, 10))
        title = tk.Frame(heading, bg=WHITE)
        title.pack(side="left")
        self._label(title, "待处理 PDF", size=12, bold=True).pack(anchor="w")
        self._label(title, color=MUTED, size=9, textvariable=self.source_mode_var).pack(anchor="w")
        self._label(heading, color=MUTED, size=9, textvariable=self.count_var).pack(side="right")
        search_row = tk.Frame(card, bg=WHITE, highlightbackground=BORDER, highlightthickness=1)
        search_row.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        search_row.grid_columnconfigure(1, weight=1)
        tk.Label(search_row, image=self._icon("search", size=16, color=MUTED), bg=WHITE).grid(
            row=0, column=0, padx=(10, 5), pady=4)
        self.search_entry = tk.Entry(search_row, textvariable=self.search_var, relief="flat",
                                     bg=WHITE, fg=TEXT, font=("Segoe UI", 10))
        self.search_entry.grid(row=0, column=1, sticky="ew", pady=7)
        self.search_placeholder = tk.Label(search_row, text="筛选文件...", bg=WHITE, fg="#98A9C1",
                                            font=("Segoe UI", 10), cursor="xterm")
        self.search_placeholder.place(x=36, rely=0.5, anchor="w")
        self.search_placeholder.bind("<Button-1>", lambda _event: self.search_entry.focus_set())
        self.search_entry.bind("<FocusIn>", self._update_search_placeholder)
        self.search_entry.bind("<FocusOut>", self._update_search_placeholder)
        tree_box = tk.Frame(card, bg=WHITE)
        tree_box.grid(row=2, column=0, sticky="nsew", padx=(14, 14))
        tree_box.grid_rowconfigure(0, weight=1)
        tree_box.grid_columnconfigure(0, weight=1)
        self.pdf_tree = ttk.Treeview(tree_box, style="Card.Treeview", selectmode="browse",
                                     columns=("file", "pages", "bookmarks", "action"),
                                     displaycolumns=("file", "pages", "bookmarks"), show="tree headings")
        self.pdf_tree.heading("#0", text="序号")
        self.pdf_tree.heading("file", text="文件")
        self.pdf_tree.heading("pages", text="页")
        self.pdf_tree.heading("bookmarks", text="原书签")
        self.pdf_tree.column("#0", width=62, minwidth=56, stretch=False, anchor="center")
        self.pdf_tree.column("file", width=170, minwidth=100, stretch=True)
        self.pdf_tree.column("pages", width=45, minwidth=40, stretch=False, anchor="center")
        self.pdf_tree.column("bookmarks", width=65, minwidth=58, stretch=False, anchor="center")
        self.pdf_tree.column("action", width=54, minwidth=50, stretch=False, anchor="center")
        self.pdf_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(tree_box, orient="vertical", style="App.Vertical.TScrollbar",
                                  command=self.pdf_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.pdf_tree.configure(yscrollcommand=scrollbar.set)
        self.pdf_tree.bind("<<TreeviewSelect>>", self._on_pdf_selected)
        hint = tk.Frame(card, bg=COLORS.surface_tint)
        hint.grid(row=3, column=0, sticky="ew", padx=14, pady=(9, 11))
        tk.Label(hint, image=self._icon("info", size=15), bg=COLORS.surface_tint).pack(
            side="left", padx=(8, 8), pady=7)
        self.list_hint_label = tk.Label(hint, textvariable=self.list_hint_var,
                                        bg=COLORS.surface_tint, fg=MUTED,
                                        font=("Segoe UI", 9), anchor="w", wraplength=340,
                                        justify="left")
        self.list_hint_label.pack(side="left", fill="x", expand=True)
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
        card.grid(row=0, column=1, sticky="nsew", padx=6)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(3, weight=1)
        heading = tk.Frame(card, bg=WHITE)
        heading.grid(row=0, column=0, sticky="ew", padx=15, pady=(11, 7))
        self._badge(heading, "eye").pack(side="left", padx=(0, 10))
        self._label(heading, "首页预览", size=12, bold=True).pack(side="left")
        self.preview_title_label = self._label(
            card, color=TEXT, size=10, bold=True, textvariable=self.preview_title_var,
            anchor="w", justify="left", wraplength=390)
        self.preview_title_label.grid(row=1, column=0, sticky="ew", padx=15)
        self.preview_meta_label = self._label(
            card, color=MUTED, size=9, textvariable=self.preview_meta_var,
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
        self.zoom_in_button = RoundedButton(toolbar, text="", icon_name="plus", width=36,
                                             height=34, command=lambda: self._step_zoom(25))
        self.zoom_in_button.pack(side="right", padx=(5, 5))
        self.zoom_box = ttk.Combobox(toolbar, textvariable=self.zoom_var, width=5,
                                     values=("50%", "75%", "100%", "125%", "150%", "200%", "250%"),
                                     state="readonly", justify="center", style="App.TCombobox")
        self.zoom_box.pack(side="right")
        self.zoom_box.bind("<<ComboboxSelected>>", self._on_zoom_selected)
        self.zoom_out_button = RoundedButton(toolbar, text="", icon_name="minus", width=36,
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
        card.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(2, weight=1)
        heading = tk.Frame(card, bg=WHITE)
        heading.grid(row=0, column=0, sticky="ew", padx=15, pady=(11, 8))
        self._badge(heading, "bookmark").pack(side="left", padx=(0, 10))
        self._label(heading, "现有书签", size=12, bold=True).pack(side="left")
        self._label(heading, color=MUTED, size=9, textvariable=self.outline_count_var).pack(
            side="right")
        self.outline_action_label = tk.Label(
            card, textvariable=self.outline_action_var, bg="#EEF3FA", fg=MUTED,
            font=("Segoe UI", 9), anchor="w", justify="left", wraplength=220,
            padx=9, pady=5)
        self.outline_action_label.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        tree_box = tk.Frame(card, bg=WHITE)
        tree_box.grid(row=2, column=0, sticky="nsew", padx=14)
        tree_box.grid_rowconfigure(0, weight=1)
        tree_box.grid_columnconfigure(0, weight=1)
        self.outline_tree = ttk.Treeview(tree_box, style="Card.Treeview", show="tree headings",
                                         columns=("title", "page"), selectmode="browse")
        self.outline_tree.heading("#0", text="#")
        self.outline_tree.heading("title", text="标题")
        self.outline_tree.heading("page", text="页码")
        self.outline_tree.column("#0", width=46, minwidth=40, stretch=False, anchor="center")
        self.outline_tree.column("title", width=220, minwidth=130, stretch=True)
        self.outline_tree.column("page", width=55, minwidth=48, stretch=False, anchor="center")
        self.outline_tree.grid(row=0, column=0, sticky="nsew")
        self.outline_tree.tag_configure("alternate", background=COLORS.surface_tint)
        scrollbar = ttk.Scrollbar(tree_box, orient="vertical", style="App.Vertical.TScrollbar",
                                  command=self.outline_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.outline_tree.configure(yscrollcommand=scrollbar.set)
        self.outline_hint_label = self._label(
            card, color=MUTED, size=9, textvariable=self.outline_hint_var,
            anchor="w", wraplength=220)
        self.outline_hint_label.grid(row=3, column=0, sticky="ew", padx=14, pady=(9, 11))
        card.bind("<Configure>", self._resize_outline_labels)

    def _resize_outline_labels(self, event: tk.Event) -> None:
        width = max(event.width - 48, 120)
        if width == self._outline_label_wrap_width:
            return
        self._outline_label_wrap_width = width
        self.outline_action_label.configure(wraplength=width)
        self.outline_hint_label.configure(wraplength=width)

    def _build_settings(self, card: tk.Frame) -> None:
        row = tk.Frame(card, bg=WHITE)
        row.pack(fill="x", padx=15, pady=(11, 6))
        self._badge(row, "settings", size=17).pack(side="left", padx=(0, 10))
        self._label(row, "DeepSeek 设置", bold=True).pack(side="left", padx=(0, 20))
        self._label(row, "DeepSeek API Key", color=MUTED).pack(side="left")
        self.api_key_entry = ttk.Entry(row, textvariable=self.api_key_var, show="*",
                                       width=29, style="App.TEntry")
        self.api_key_entry.pack(side="left", fill="x", expand=True, padx=(7, 7))
        self.api_key_entry.bind("<FocusOut>", self._persist_api_key)
        self._show_api_key = False
        self.api_eye_button = ttk.Button(row, image=self._icon("eye", size=16, color=MUTED),
                                          style="App.TButton", width=3,
                                          command=self._toggle_api_key)
        self.api_eye_button.pack(side="left")
        options = tk.Frame(card, bg=WHITE)
        options.pack(fill="x", padx=15, pady=(0, 3))
        self.dry_checkbox = ttk.Checkbutton(options, text="仅分析，不生成 PDF",
                                             variable=self.dry_run_var, style="App.TCheckbutton")
        self.dry_checkbox.pack(side="left", padx=(0, 19))
        self.replace_checkbox = ttk.Checkbutton(options, text="强制替换已有书签",
                                                 variable=self.replace_var, style="App.TCheckbutton")
        self.replace_checkbox.pack(side="left", padx=(0, 19))
        self.verify_checkbox = ttk.Checkbutton(options, text="按目录核对已有书签",
                                                variable=self.verify_var, style="App.TCheckbutton")
        self.verify_checkbox.pack(side="left", padx=(0, 19))
        self.resume_checkbox = ttk.Checkbutton(options, text="续跑：跳过已完成的书",
                                                variable=self.resume_var, style="App.TCheckbutton")
        self.resume_checkbox.pack(side="left")
        self._label(options, "密钥加密保存；目录核对可能产生 API 费用", color=BLUE,
                    size=9).pack(side="right")
        destination_options = tk.Frame(card, bg=WHITE)
        destination_options.pack(fill="x", padx=15, pady=(0, 10))
        self.skip_bookmarked_checkbox = ttk.Checkbutton(
            destination_options, text="已有书签就跳过（不核对）",
            variable=self.skip_bookmarked_var, style="App.TCheckbutton")
        self.skip_bookmarked_checkbox.pack(side="left", padx=(0, 19))
        self.overwrite_original_checkbox = ttk.Checkbutton(
            destination_options, text="直接覆盖原 PDF（仅成功时替换）",
            variable=self.overwrite_original_var, style="App.TCheckbutton")
        self.overwrite_original_checkbox.pack(side="left")
        self._label(destination_options, "跳过优先于强制替换；单本和整批均适用",
                    color=MUTED, size=9).pack(side="left", padx=(14, 0))

    def _toggle_api_key(self) -> None:
        self._show_api_key = not self._show_api_key
        self.api_key_entry.configure(show="" if self._show_api_key else "*")

    def _persist_api_key(self, _event: object = None) -> None:
        value = self.api_key_var.get().strip()
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

    def _on_replace_changed(self, *_: object) -> None:
        if not self._running:
            self._load_source(force=True)

    def _on_overwrite_changed(self, *_: object) -> None:
        self.output_label.configure(
            text="输出（报告/缓存）" if self.overwrite_original_var.get() else "输出")
        self._on_replace_changed()

    def _on_path_committed(self, _event: object = None) -> None:
        if not self._running:
            self._load_source()

    def _current_signature(self) -> tuple[object, ...]:
        return (
            self.input_var.get().strip(), self.output_var.get().strip(),
            self.replace_var.get(), self.verify_var.get(),
            self.skip_bookmarked_var.get(), self.resume_var.get(),
            self.overwrite_original_var.get(),
            self.dry_run_var.get(),
        )

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
            self._suggested_output = str(Path(chosen).parent / (Path(chosen).name + "_bookmarked"))
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
            self._suggested_output = str(Path(chosen).parent / "bookmarked")
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

    def _load_source(self, *, force: bool = False) -> None:
        input_text = self.input_var.get().strip()
        output_text = self.output_var.get().strip()
        signature = self._current_signature()
        if not force and signature == self._loaded_signature:
            return
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
        self._file_indices.clear()
        self._cover_cache.clear()
        self.pdf_tree.delete(*self.pdf_tree.get_children())
        self._clear_preview()
        self._scan_skipped = 0
        self._scan_failed = 0
        self._scan_resumed = 0

        if not input_text or not output_text:
            self.count_var.set("尚未选择 PDF")
            self.source_mode_var.set("选择 PDF 或文件夹")
            self.list_hint_var.set("选择单个 PDF 或文件夹")
            self.status_var.set("请选择来源和输出位置")
            self._update_start_state()
            return
        source = Path(input_text).expanduser().resolve()
        output = Path(output_text).expanduser().resolve()
        if source.is_file() and source.suffix.lower() == ".pdf":
            self._source_is_file = True
            self.source_mode_var.set("单个 PDF")
        elif source.is_dir():
            self._source_is_file = False
            self.source_mode_var.set("文件夹")
        else:
            self.count_var.set("未找到 PDF")
            self.source_mode_var.set("无效来源")
            self.list_hint_var.set("检查输入路径是否指向 PDF 文件或文件夹")
            self.status_var.set("输入路径无效")
            self._update_start_state()
            return
        if not self._source_is_file and (
            source.is_relative_to(output) or output.is_relative_to(source)
        ):
            self.count_var.set("输出位置无效")
            self.list_hint_var.set("输入和输出文件夹不能相同，也不能互相包含")
            self.status_var.set("请更换输出位置")
            self._update_start_state()
            return
        self._source_path = source
        self._scanning = True
        self.count_var.set("正在读取 PDF…")
        self.list_hint_var.set("正在检查页数和现有书签")
        self.status_var.set("扫描中…")
        self._update_start_state()
        threading.Thread(target=self._scan_source, args=(generation, source, output,
                         self._source_is_file, self.replace_var.get(), self.verify_var.get(),
                         self.skip_bookmarked_var.get(), self.resume_var.get(),
                         self.overwrite_original_var.get(), self.dry_run_var.get(),
                         self._scan_cancel),
                         daemon=True).start()

    def _scan_source(self, generation: int, source: Path, output: Path, single: bool,
                     replace_existing: bool, verify_existing: bool,
                     skip_bookmarked: bool, resume: bool,
                     overwrite_original: bool, dry_run: bool,
                     cancel: threading.Event) -> None:
        try:
            paths = [source] if single else list_pdf_paths(source, output)
            self._events.put(("scan_total", (generation, len(paths))))
            done = _done_files(output / "bookmarker-report.jsonl") if resume else {}
            source_dir = source.parent if single else source
            skipped = failed = resumed = 0
            for index, path in enumerate(paths, 1):
                if cancel.is_set():
                    return
                old = done.get(str(path))
                resume_skip = (resume and _resume_skips(
                    path, source_dir, output, old,
                    dry_run=dry_run, replace_existing=replace_existing,
                    verify_existing=verify_existing,
                    skip_bookmarked=skip_bookmarked,
                    overwrite_original=overwrite_original))
                if resume_skip and not single:
                    resumed += 1
                    continue
                preview = inspect_pdf(path, replace_existing=replace_existing,
                                      verify_existing=verify_existing,
                                      skip_bookmarked=skip_bookmarked,
                                      include_bookmarks=single)
                if resume_skip:
                    preview = replace(preview, eligible=False, reason="续跑已完成，默认跳过")
                if single or preview.eligible:
                    self._events.put(("scan_item", (generation, index, preview)))
                elif preview.reason.startswith("已有"):
                    skipped += 1
                else:
                    failed += 1
            self._events.put(("scan_done", (generation, skipped, failed, resumed)))
        except Exception as error:
            self._events.put(("scan_error", (generation, str(error))))

    def _handle_scan_item(self, generation: int, index: int, preview: PdfPreview) -> None:
        if generation != self._scan_generation:
            return
        path_text = str(preview.path)
        self._previews[path_text] = preview
        self._file_indices[path_text] = index
        if self._source_path and not self._source_is_file:
            try:
                name = str(preview.path.relative_to(self._source_path))
            except ValueError:
                name = preview.path.name
        else:
            name = preview.path.name
        query = self.search_var.get().strip().casefold()
        if not query or query in name.casefold():
            self.pdf_tree.insert("", "end", iid=path_text, text=str(index), image=self._pdf_icon,
                                 values=(name, preview.page_count or "—", preview.bookmark_count or "—",
                                         _processing_label(preview,
                                                           replace_existing=self.replace_var.get(),
                                                           verify_existing=self.verify_var.get(),
                                                           dry_run=self.dry_run_var.get())))
            if not self.pdf_tree.selection():
                self.pdf_tree.selection_set(path_text)
                self.pdf_tree.focus(path_text)
                self._show_preview(preview)
        self.count_var.set("共 1 个文件" if self._source_is_file
                           else f"共 {len(self._previews)} 个文件")

    def _rebuild_list(self) -> None:
        if not hasattr(self, "pdf_tree"):
            return
        selected = self.pdf_tree.selection()
        previous = selected[0] if selected else None
        self.pdf_tree.delete(*self.pdf_tree.get_children())
        query = self.search_var.get().strip().casefold()
        for path_text, preview in self._previews.items():
            if self._source_path and not self._source_is_file:
                try:
                    name = str(preview.path.relative_to(self._source_path))
                except ValueError:
                    name = preview.path.name
            else:
                name = preview.path.name
            if query and query not in name.casefold():
                continue
            self.pdf_tree.insert("", "end", iid=path_text,
                                 text=str(self._file_indices[path_text]), image=self._pdf_icon,
                                 values=(name, preview.page_count or "—", preview.bookmark_count or "—",
                                         _processing_label(preview,
                                                           replace_existing=self.replace_var.get(),
                                                           verify_existing=self.verify_var.get(),
                                                           dry_run=self.dry_run_var.get())))
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
        if self._source_is_file:
            self.count_var.set("共 1 个文件")
        else:
            self.count_var.set(f"共 {len(self._previews)} 个文件")
        self._update_start_state()

    def _on_pdf_selected(self, _event: object = None) -> None:
        selected = self.pdf_tree.selection()
        if selected and selected[0] in self._previews:
            preview = self._previews[selected[0]]
            if preview.path != self._displayed_path:
                self._show_preview(preview)

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
        self.preview_title_var.set(preview.path.name)
        self.preview_meta_var.set(
            f"{preview.page_count} 页  ·  {_file_size(preview.path)}  ·  "
            f"现有 {preview.bookmark_count} 个书签")
        self.outline_count_var.set(f"共 {preview.bookmark_count} 条")
        self.outline_action_var.set(
            _display_reason(preview, dry_run=self.dry_run_var.get()))
        if preview.eligible and _processing_label(
                preview, replace_existing=self.replace_var.get(),
                verify_existing=self.verify_var.get(),
                dry_run=self.dry_run_var.get()) == "替换":
            self.outline_action_label.configure(bg="#FFF4E8", fg="#975416")
        elif preview.eligible:
            self.outline_action_label.configure(bg=PALE_BLUE, fg=BLUE)
        else:
            self.outline_action_label.configure(bg="#EEF3FA", fg=MUTED)
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
                             args=(selection_generation, preview.path, self.replace_var.get(),
                                   self.verify_var.get(), self.skip_bookmarked_var.get()),
                             daemon=True).start()
        self._request_page(preview.path, 1)

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

    def _read_selected_outline(self, generation: int, path: Path,
                               replace_existing: bool, verify_existing: bool,
                               skip_bookmarked: bool) -> None:
        detail = inspect_pdf(path, replace_existing=replace_existing,
                             verify_existing=verify_existing,
                             skip_bookmarked=skip_bookmarked)
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
            "可展开层级查看原有书签" if nodes else "这本 PDF 目前没有书签")

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
        self.outline_action_label.configure(bg="#EEF3FA", fg=MUTED)
        self.outline_hint_var.set("选择文件后显示原有书签")
        self.outline_tree.delete(*self.outline_tree.get_children())
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
                               fill=MUTED, font=("Segoe UI", 10), width=max(width - 35, 20),
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
        ready = bool(self._previews) and not self._scanning and not self._running
        if self._source_is_file and self._previews:
            ready = ready and next(iter(self._previews.values())).eligible
        self.start_button.configure(state="normal" if ready else "disabled",
                                    text="为此 PDF 添加书签" if self._source_is_file else "批量添加书签")

    def _set_running(self, running: bool) -> None:
        self._running = running
        entry_state = "disabled" if running else "normal"
        for widget in (self.input_entry, self.output_entry, self.input_button, self.file_button,
                       self.output_button, self.clear_input_button, self.clear_output_button,
                       self.dry_checkbox, self.replace_checkbox, self.verify_checkbox,
                       self.skip_bookmarked_checkbox, self.resume_checkbox,
                       self.overwrite_original_checkbox):
            widget.configure(state=entry_state)
        self._update_api_key_controls()
        self._update_start_state()
        self.stop_button.configure(state="normal" if running else "disabled")

    def _append(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _toggle_log(self) -> None:
        self._set_log_visible(not self._log_visible)

    def _set_log_visible(self, visible: bool) -> None:
        if visible == self._log_visible:
            return
        self._log_visible = visible
        if visible:
            self.log_frame.grid(row=6, column=0, sticky="ew", pady=(7, 0))
            self.log_button.configure(text="处理日志  ▴")
        else:
            self.log_frame.grid_remove()
            self.log_button.configure(text="处理日志  ▾")

    def _start(self) -> None:
        if self._running or self._scanning:
            return
        signature = self._current_signature()
        if signature != self._loaded_signature:
            self._load_source()
            return
        input_text, output_text = signature[:2]
        if not input_text or not output_text:
            messagebox.showerror("路径未选择", "请选择 PDF 文件或文件夹，以及输出文件夹。", parent=self.root)
            return
        input_path = Path(input_text).expanduser().resolve()
        output_dir = Path(output_text).expanduser().resolve()
        single_file = input_path.is_file()
        if single_file and input_path.suffix.lower() != ".pdf":
            messagebox.showerror("输入文件无效", f"请选择 PDF 文件：\n{input_path}", parent=self.root)
            return
        if not single_file and not input_path.is_dir():
            messagebox.showerror("输入路径无效", f"找不到 PDF 文件或文件夹：\n{input_path}", parent=self.root)
            return
        if output_dir.exists() and not output_dir.is_dir():
            messagebox.showerror("输出路径无效", f"请选择输出文件夹：\n{output_dir}", parent=self.root)
            return
        if not single_file and (input_path.is_relative_to(output_dir)
                                or output_dir.is_relative_to(input_path)):
            messagebox.showerror("输出文件夹无效", "输入和输出文件夹不能相同，也不能互相包含。",
                                 parent=self.root)
            return
        if not self._previews:
            messagebox.showinfo("没有待处理 PDF", "此位置没有需要添加书签的 PDF。", parent=self.root)
            return
        if single_file and not next(iter(self._previews.values())).eligible:
            return
        api_key = self.api_key_var.get().strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            messagebox.showerror("缺少 DeepSeek API Key",
                                 "请输入 DeepSeek API Key，或设置 DEEPSEEK_API_KEY 环境变量。",
                                 parent=self.root)
            return

        self._persist_api_key()

        environment = build_batch_environment(api_key=api_key)
        try:
            command = build_batch_command(
                input_path, output_dir, single_file=single_file,
                dry_run=self.dry_run_var.get(), replace_existing=self.replace_var.get(),
                verify_existing=self.verify_var.get(),
                skip_bookmarked=self.skip_bookmarked_var.get(),
                overwrite_original=self.overwrite_original_var.get(),
                resume=self.resume_var.get())
            self._process = subprocess.Popen(
                command, cwd=self.working_dir, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            messagebox.showerror("启动失败", str(exc), parent=self.root)
            return
        self._report_path = output_dir / REPORT_NAME
        self._report_before = _file_fingerprint(self._report_path)
        self.report_var.set(f"报告：{self._report_path}")
        self.report_button.configure(state="disabled")
        self.status_var.set("处理中…")
        self._append(f"开始：{input_path} → {'原 PDF（直接覆盖）' if self.overwrite_original_var.get() and not self.dry_run_var.get() else output_dir}")
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
                generation, total = value
                if generation == self._scan_generation:
                    self.count_var.set(f"正在检查 {total} 本 PDF…")
                    ordinal_width = max(62, 39 + len(str(max(total, 1))) * 8)
                    self.pdf_tree.column("#0", width=ordinal_width, minwidth=ordinal_width)
            elif kind == "scan_item":
                generation, index, preview = value
                self._handle_scan_item(generation, index, preview)
            elif kind == "scan_done":
                generation, skipped, failed, resumed = value
                if generation == self._scan_generation:
                    self._scanning = False
                    self._scan_skipped, self._scan_failed = skipped, failed
                    self._scan_resumed = resumed
                    self._rebuild_list()
                    notes = []
                    if skipped:
                        notes.append(f"{skipped} 本因已有书签跳过" if self.skip_bookmarked_var.get()
                                     else f"{skipped} 本已有完整书签")
                    if failed:
                        notes.append(f"{failed} 本无法读取")
                    if resumed:
                        notes.append(f"{resumed} 本按续跑设置跳过")
                    if self._source_is_file and self._previews:
                        hint = "当前仅处理这本 PDF；首页与原书签可在右侧预览"
                    elif self._previews:
                        hint = "选中只切换预览；批量处理全部待处理或待核对 PDF"
                    else:
                        hint = "没有需要添加书签的 PDF"
                    if notes:
                        hint += " · " + "；".join(notes)
                    self.list_hint_var.set(hint)
                    if self._source_is_file and self._previews:
                        only = next(iter(self._previews.values()))
                        self.status_var.set(
                            f"{_processing_label(only, replace_existing=self.replace_var.get(), verify_existing=self.verify_var.get(), dry_run=self.dry_run_var.get())}书签"
                            if only.eligible else only.reason)
                        if not only.eligible:
                            self.list_hint_var.set(
                                ("取消“已有书签就跳过（不核对）”后可重新处理；文件预览仍可查看"
                                 if self.skip_bookmarked_var.get() else
                                 "勾选“强制替换已有书签”可重新生成；文件预览仍可查看")
                                if only.reason.startswith("已有") else only.reason)
                    else:
                        self.status_var.set(f"已找到 {len(self._previews)} 本待处理或待核对 PDF")
                    self._update_start_state()
            elif kind == "scan_error":
                generation, detail = value
                if generation == self._scan_generation:
                    self._scanning = False
                    self.count_var.set("扫描失败")
                    self.list_hint_var.set(detail)
                    self.status_var.set("无法读取来源")
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
                    self._previews[str(path)] = detail
                    self._populate_outline(detail.bookmarks)
            elif kind == "line":
                self._append(str(value))
            elif kind == "done":
                exit_code = int(value)
                self._process = None
                self._set_running(False)
                self.status_var.set("处理完成" if exit_code == 0 else f"结束（退出码 {exit_code}）")
                if (self._report_path
                        and _file_fingerprint(self._report_path) is not None
                        and _file_fingerprint(self._report_path) != self._report_before):
                    self.report_button.configure(state="normal")
                    self._append(f"报告已保存：{self._report_path}")
                else:
                    self.report_var.set("本次没有生成报告")
                    self._append("没有生成报告。请查看上面的错误信息。")
                if self.overwrite_original_var.get() and not self.dry_run_var.get():
                    self._load_source(force=True)
        self._poll_after = self.root.after(100, self._poll_events)

    def _stop(self) -> None:
        if self._process and self._process.poll() is None:
            self.status_var.set("正在停止…")
            self.stop_button.configure(state="disabled")
            self._process.terminate()

    def _open_report(self) -> None:
        if self._report_path and self._report_path.is_file():
            try:
                os.startfile(self._report_path)  # type: ignore[attr-defined]
            except OSError as exc:
                messagebox.showerror("无法打开报告", str(exc), parent=self.root)

    def _close(self) -> None:
        if self._running:
            if not messagebox.askyesno("停止处理？", "处理尚未结束。停止并关闭窗口？",
                                       parent=self.root):
                return
            self._stop()
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
    BookmarkApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
