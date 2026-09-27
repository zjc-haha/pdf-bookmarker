"""Interactive workspace checks without starting a recognition worker."""

from __future__ import annotations

import io
import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from PIL import Image
from pypdf import PdfWriter

from bookmarker import gui
from bookmarker.preview import BookmarkNode, PdfPreview


class WorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.root.withdraw()
        self._load_key_patch = patch.object(gui, "load_key", return_value="")
        self._save_key_patch = patch.object(gui, "save_key")
        self._load_key_patch.start()
        self.mock_save_key = self._save_key_patch.start()
        self.app = gui.BookmarkApp(self.root)

    def tearDown(self) -> None:
        if hasattr(self, "app") and self.root.winfo_exists():
            self.app._close()
        self._save_key_patch.stop()
        self._load_key_patch.stop()

    def _until(self, predicate, timeout: float = 4) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("GUI did not reach expected state")

    def _skip_initial_source_load(self) -> None:
        if self.app._load_after:
            self.root.after_cancel(self.app._load_after)
            self.app._load_after = None

    def test_header_shows_version_and_help_opens_usage_guide(self) -> None:
        self._skip_initial_source_load()
        # Windows keeps a transient window hidden while its owner is withdrawn.
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.root.update()
        self.assertEqual(self.app.version_label.cget("text"), f"v{gui.__version__}")
        self.app.root.event_generate("<F1>", when="now")
        self.app._show_help()
        self.root.update()
        window = self.app.help_window
        self.assertIsNotNone(window)
        self.assertEqual(window.title(), "使用帮助")
        self.assertEqual(window.state(), "normal")
        self.assertIn(gui.__version__, self.app.help_version_label.cget("text"))
        texts = []

        def collect(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if isinstance(child, tk.Label):
                    texts.append(str(child.cget("text")))
                collect(child)

        collect(window)
        joined = "".join(texts).replace("\n", "")
        for phrase in ("确认并写入书签", "data 文件夹", "DeepSeek"):
            self.assertIn(phrase, joined)
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            readme.write_text("# 说明", encoding="utf-8")
            with patch.object(self.app, "_readme_path", return_value=readme), \
                 patch.object(gui.os, "startfile", create=True) as startfile:
                self.app._open_readme()
            startfile.assert_called_once_with(readme)
        buttons = {getattr(button, "_text", ""): button
                   for child in window.winfo_children() for button in child.winfo_children()}
        with patch.object(gui.webbrowser, "open") as browse:
            buttons["查看新版本"].invoke()
        browse.assert_called_once_with(gui.RELEASES_URL)
        buttons["关闭"].invoke()
        self.root.update()
        self.assertEqual(window.state(), "withdrawn")
        self.app._show_help()
        self.assertIs(self.app.help_window, window)

    def test_deepseek_is_the_only_recognition_option(self) -> None:
        self._skip_initial_source_load()
        self.assertFalse(hasattr(self.app, "engine_box"))
        self.assertFalse(hasattr(self.app, "ocr_box"))
        self.assertFalse(hasattr(self.app, "engine_var"))
        self.assertFalse(hasattr(self.app, "ocr_var"))
        pending = [self.root]
        labels = []
        while pending:
            widget = pending.pop()
            if isinstance(widget, tk.Label):
                labels.append(widget.cget("text"))
            pending.extend(widget.winfo_children())
        self.assertIn("DEEPSEEK", labels)
        self.assertNotIn("OCR", " ".join(labels).upper())
        self.assertEqual(str(self.app.api_key_entry.cget("state")), "normal")
        self.app._set_running(True)
        self.assertEqual(str(self.app.api_key_entry.cget("state")), "disabled")
        self.app._set_running(False)
        self.assertEqual(str(self.app.api_key_entry.cget("state")), "normal")

    def test_skip_bookmarked_keeps_skipped_files_visible(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "books"
            folder.mkdir()
            bookmarked = folder / "has-bookmark.pdf"
            empty = folder / "no-bookmark.pdf"
            for path, with_bookmark in ((bookmarked, True), (empty, False)):
                writer = PdfWriter()
                writer.add_blank_page(width=200, height=300)
                if with_bookmark:
                    writer.add_outline_item("1", 0)
                with path.open("wb") as stream:
                    writer.write(stream)
            with patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60))):
                self.app.skip_bookmarked_var.set(True)
                self.app.input_var.set(str(folder))
                self.app.output_var.set(str(root / "reports"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning and
                            self.app.pdf_tree.get_children() == (str(bookmarked), str(empty)))
                self.assertEqual(self.app._scan_skipped, 1)
                self.assertEqual(self.app.pdf_tree.set(str(bookmarked), "action"), "跳过")
                self.assertIn("预计识别 1", self.app.run_summary_var.get())

    def test_directory_includes_complete_looking_books_for_toc_verification(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "books"
            folder.mkdir()
            source = folder / "apparently-complete.pdf"
            source.touch()

            def inspect(path, *, verify_existing=False, include_bookmarks=True,
                        replace_existing=False, skip_bookmarked=False):
                return PdfPreview(Path(path), 40, 30, 1.0, verify_existing,
                                  "待与印刷目录核对" if verify_existing else "已有完整书签")

            with patch.object(gui, "inspect_pdf", side_effect=inspect) as scan, \
                 patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60))):
                self.app.input_var.set(str(folder))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning and self.app._cover_image is not None)
                self.assertEqual(self.app.pdf_tree.get_children(), (str(source),))
                self.assertEqual(self.app.pdf_tree.set(str(source), "action"), "核对")
                self.assertEqual(self.app.start_button["state"], "normal")
                self.assertTrue(any(call.kwargs.get("verify_existing") for call in scan.call_args_list))

    def test_single_file_shows_cover_and_existing_bookmark_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "complete.pdf"
            source.touch()
            outline = (BookmarkNode("Chapter 1", 1, (BookmarkNode("Section 1.1", 2),)),)

            def inspect(path, *, replace_existing=False, verify_existing=False,
                        include_bookmarks=True, skip_bookmarked=False):
                return PdfPreview(Path(path), 40, 30, 1.0, replace_existing,
                                  "待添加书签" if replace_existing else "已有 30 条可用章节书签",
                                  outline if include_bookmarks else ())

            with patch.object(gui, "inspect_pdf", side_effect=inspect), \
                 patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60))):
                self.app.verify_var.set(False)
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning and self.app._cover_image is not None)

                self.assertEqual(len(self.app.pdf_tree.get_children()), 1)
                self.assertEqual(self.app.pdf_tree.item(str(source), "text"), "1")
                self.assertEqual(self.app.pdf_tree.set(str(source), "file"), source.name)
                self.assertEqual(self.app.preview_title_var.get(), source.name)
                self.assertEqual(self.app._cover_image.size, (40, 60))
                root_nodes = self.app.outline_tree.get_children()
                self.assertEqual(len(root_nodes), 1)
                self.assertEqual(self.app.outline_tree.item(root_nodes[0], "text"), "1")
                self.assertEqual(self.app.outline_tree.set(root_nodes[0], "title"), "Chapter 1")
                self.assertEqual(self.app.outline_tree.set(
                    self.app.outline_tree.get_children(root_nodes[0])[0], "title").strip(),
                    "Section 1.1")
                self.assertEqual(str(self.app.start_button["state"]), "disabled")

                self.app.replace_var.set(True)
                self._until(lambda: not self.app._scanning and
                            str(self.app.start_button["state"]) == "normal")

    def test_directory_lists_candidates_and_selection_updates_cover_and_bookmarks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "books"
            source.mkdir()
            for name in ("a.pdf", "b.pdf", "complete.pdf"):
                (source / name).touch()
            chapter = (BookmarkNode("Original chapter", 2),)

            def inspect(path, *, replace_existing=False, verify_existing=False,
                        include_bookmarks=True, skip_bookmarked=False):
                path = Path(path)
                if path.name == "complete.pdf":
                    return PdfPreview(path, 35, 30, 1.0, replace_existing,
                                      "已有 30 条可用章节书签")
                count = 1 if path.name == "b.pdf" else 0
                if count and include_bookmarks:
                    time.sleep(0.15)
                return PdfPreview(path, 10, count, 0.0, True, "待添加书签",
                                  chapter if count and include_bookmarks else ())

            def render(path, *, max_size=1000):
                if Path(path).name == "a.pdf":
                    time.sleep(0.12)
                color = "green" if Path(path).name == "b.pdf" else "red"
                return Image.new("RGB", (40, 60), color)

            with patch.object(gui, "inspect_pdf", side_effect=inspect), \
                 patch.object(gui, "render_first_page", side_effect=render):
                self.app.verify_var.set(False)
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning and
                            len(self.app.pdf_tree.get_children()) == 3)

                names = {Path(item).name for item in self.app.pdf_tree.get_children()}
                self.assertEqual(names, {"a.pdf", "b.pdf", "complete.pdf"})
                self.assertEqual(str(self.app.start_button["state"]), "normal")
                selected = str(source / "b.pdf")
                self.assertEqual(self.app.pdf_tree.set(selected, "action"), "替换")
                self.app.pdf_tree.selection_set(selected)
                self.app._on_pdf_selected()
                self._until(lambda: self.app.preview_title_var.get() == "b.pdf" and
                            self.app._cover_image is not None and
                            self.app._cover_image.getpixel((0, 0)) == (0, 128, 0))
                # Change selection before B's outline detail arrives, then
                # revisit its already cached cover. The outline must still load.
                self.app.pdf_tree.selection_set(str(source / "a.pdf"))
                self.app._on_pdf_selected()
                self.app.pdf_tree.selection_set(selected)
                self.app._on_pdf_selected()
                self._until(lambda: len(self.app.outline_tree.get_children()) == 1)
                self.assertIn("现有 1 条书签", self.app.outline_action_var.get())
                self.assertEqual(self.app.outline_tree.set(
                    self.app.outline_tree.get_children()[0], "title"), "Original chapter")
                # The slower A render finishes after B is selected. Its stale
                # result must not replace the currently selected cover.
                time.sleep(0.2)
                self.root.update()
                self.assertEqual(self.app._cover_image.getpixel((0, 0)), (0, 128, 0))

                self.app.search_var.set("b.pdf")
                self.assertEqual(self.app.pdf_tree.get_children(), (selected,))

    def test_directory_numbers_match_cli_order_after_skips_and_search(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "books"
            source.mkdir()
            for name in ("c.pdf", "b.pdf", "a.pdf"):
                (source / name).touch()

            def inspect(path, *, replace_existing=False, verify_existing=False,
                        include_bookmarks=True, skip_bookmarked=False):
                path = Path(path)
                if path.name == "b.pdf":
                    return PdfPreview(path, 12, 30, 1.0, False, "已有完整书签")
                return PdfPreview(path, 12, 0, 0.0, True, "待添加书签")

            with patch.object(gui, "inspect_pdf", side_effect=inspect), \
                 patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60))):
                self.app.verify_var.set(False)
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning and
                            len(self.app.pdf_tree.get_children()) == 3)

                a, b, c = (str(source / name) for name in ("a.pdf", "b.pdf", "c.pdf"))
                self.assertEqual(self.app.pdf_tree.get_children(), (a, b, c))
                self.assertEqual(self.app.pdf_tree.heading("#0", "text"), "序号")
                self.assertEqual(self.app.pdf_tree.item(a, "text"), "1")
                self.assertEqual(self.app.pdf_tree.item(b, "text"), "2")
                self.assertEqual(self.app.pdf_tree.item(c, "text"), "3")
                self.assertEqual(self.app.pdf_tree.set(c, "file"), "c.pdf")

                self.app.filter_var.set("待处理")
                self.assertEqual(self.app.pdf_tree.get_children(), (a, c))
                self.app.filter_var.set("全部")

                self.app.search_var.set("c.pdf")
                self.assertEqual(self.app.pdf_tree.get_children(), (c,))
                self.assertEqual(self.app.pdf_tree.item(c, "text"), "3")
                self.app.search_var.set("")
                self.assertEqual(self.app.pdf_tree.get_children(), (a, b, c))
                self.assertEqual(self.app.pdf_tree.item(c, "text"), "3")

    def test_page_navigation_uses_requested_page_and_cached_first_page(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "pages.pdf"
            source.touch()
            preview = PdfPreview(source, 3, 0, 0.0, True, "待添加书签")

            def render_page(_path, page_number, *, max_size=1000):
                return Image.new("RGB", (40, 60), {2: "green", 3: "blue"}[page_number])

            with patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60), "red")) as first, \
                 patch.object(gui, "render_page", side_effect=render_page) as later:
                self.app._show_preview(preview)
                self._until(lambda: self.app._cover_image is not None)
                self.assertEqual(self.app.page_var.get(), "1")
                self.assertEqual(self.app.page_total_var.get(), "/ 3")
                self.assertTrue(self.app.prev_button.instate(["disabled"]))
                self.assertFalse(self.app.next_button.instate(["disabled"]))

                self.app._step_page(1)
                self._until(lambda: self.app._cover_image is not None and
                            self.app._cover_image.getpixel((0, 0)) == (0, 128, 0))
                self.assertEqual(self.app.page_var.get(), "2")
                self.assertEqual(later.call_args.args[1], 2)

                self.app._step_page(-1)
                self.assertEqual(self.app._cover_image.getpixel((0, 0)), (255, 0, 0))
                self.assertEqual(first.call_count, 1)

                self.app.page_var.set("not a page")
                self.app._on_page_entered()
                self.assertEqual(self.app.page_var.get(), "1")
                self.app.page_var.set("999")
                self.app._on_page_entered()
                self._until(lambda: self.app._cover_image is not None and
                            self.app._cover_image.getpixel((0, 0)) == (0, 0, 255))
                self.assertEqual(self.app.page_var.get(), "3")
                self.assertTrue(self.app.next_button.instate(["disabled"]))

    def test_zoom_controls_clamp_and_reset_invalid_selection(self) -> None:
        self._skip_initial_source_load()
        self.app._displayed_path = Path("sample.pdf")
        self.app._page_count = 2
        self.app._cover_image = Image.new("RGB", (40, 60))
        self.app._update_page_controls()

        self.app._step_zoom(25)
        self.assertEqual(self.app.zoom_var.get(), "125%")
        self.assertEqual(self.app._zoom_factor, 1.25)
        self.app.zoom_var.set("200%")
        self.app._on_zoom_selected()
        self.assertEqual(self.app._zoom_factor, 2.0)
        self.app.zoom_var.set("invalid")
        self.app._on_zoom_selected()
        self.assertEqual(self.app.zoom_var.get(), "200%")
        self.app._set_zoom(500)
        self.assertEqual(self.app.zoom_var.get(), "250%")
        self.assertTrue(self.app.zoom_in_button.instate(["disabled"]))
        self.app._set_zoom(1)
        self.assertEqual(self.app.zoom_var.get(), "50%")
        self.assertTrue(self.app.zoom_out_button.instate(["disabled"]))

    def test_fullscreen_preview_opens_updates_page_and_closes(self) -> None:
        self._skip_initial_source_load()
        self.app._displayed_path = Path("sample.pdf")
        self.app._page_count = 2
        self.app._cover_image = Image.new("RGB", (40, 60))
        self.app._update_page_controls()

        with patch.object(tk.Toplevel, "attributes", return_value=None), \
             patch.object(self.app, "_request_page") as request:
            self.app._open_fullscreen()
            window = self.app._fullscreen_window
            self.assertIsNotNone(window)
            self.assertTrue(window.winfo_exists())
            self.assertEqual(self.app.fullscreen_page_var.get(), "第 1 / 2 页")
            self.app._step_page(1)
            self.assertEqual(self.app.fullscreen_page_var.get(), "第 2 / 2 页")
            request.assert_called_once_with(Path("sample.pdf"), 2)
            self.app._close_fullscreen()
            self.assertIsNone(self.app._fullscreen_window)
            self.assertFalse(window.winfo_exists())

    def test_api_key_visibility_and_path_clear_buttons(self) -> None:
        self._skip_initial_source_load()
        self.app.api_key_var.set("sample-secret")
        self.app._persist_api_key()
        self.mock_save_key.assert_called_with("sample-secret")
        self.assertEqual(self.app.api_key_entry.cget("show"), "*")
        self.app.api_eye_button.invoke()
        self.assertEqual(self.app.api_key_entry.cget("show"), "")
        self.app.api_eye_button.invoke()
        self.assertEqual(self.app.api_key_entry.cget("show"), "*")

        self.app.input_var.set("C:/books/example.pdf")
        self.app.output_var.set("C:/books/output")
        self.app.clear_input_button.invoke()
        self.assertEqual(self.app.input_var.get(), "")
        self.assertEqual(self.app.count_var.get(), "尚未选择 PDF")
        self.app.input_var.set("C:/books/example.pdf")
        self.app.clear_output_button.invoke()
        self.assertEqual(self.app.output_var.get(), "")
        self.assertEqual(self.app.count_var.get(), "尚未选择 PDF")
        self.app.api_key_var.set("")
        self.app._persist_api_key()
        self.assertEqual(self.app.api_key_var.get(), "sample-secret")
        self.mock_save_key.assert_called_once_with("sample-secret")

    def test_overwrite_disables_output_and_ignores_its_value(self) -> None:
        self._skip_initial_source_load()
        self.assertFalse(self.app.overwrite_original_var.get())
        self.assertEqual(self.app.output_label.cget("text"), "输出")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=300)
            with source.open("wb") as stream:
                writer.write(stream)
            self.app.input_var.set(str(source))
            self.app.output_var.set("")
            self.app.overwrite_original_var.set(True)
            self._until(lambda: not self.app._scanning and
                        self.app._loaded_signature == self.app._current_signature())
            self.assertTrue(self.app.overwrite_original_checkbox.instate(["selected"]))
            self.assertIn("覆盖时不使用", self.app.output_label.cget("text"))
            for widget in (self.app.output_entry, self.app.output_button,
                           self.app.clear_output_button):
                self.assertEqual(str(widget["state"]), "disabled", str(widget))
            self.assertEqual(self.app._resolved_output_dir(),
                             (self.app.working_dir / "output").resolve())
            signature = self.app._current_signature()
            generation = self.app._scan_generation
            self.app.output_var.set(str(Path(directory) / "ignored-output"))
            self.assertEqual(self.app._current_signature(), signature)
            self.assertEqual(self.app._scan_generation, generation)
            self.assertEqual(self.app.start_button["state"], "normal")

    def test_double_click_opens_selected_pdf_with_windows_default_viewer(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.pdf"
            second = Path(directory) / "second.pdf"
            for index, source in enumerate((first, second), 1):
                source.touch()
                self.app._previews[str(source)] = PdfPreview(
                    source, 1, 0, 0.0, True, "待添加书签")
                self.app._file_indices[str(source)] = index
            self.app._rebuild_list()
            self.app.pdf_tree.selection_set(str(first))
            self.assertTrue(self.app.pdf_tree.bind("<Double-1>"))
            with patch.object(gui.os, "startfile", create=True) as startfile, \
                 patch.object(self.app.pdf_tree, "identify_row", return_value=str(second)):
                self.app._open_selected_pdf(SimpleNamespace(num=1, y=10))
            startfile.assert_called_once_with(second)

    def test_worker_log_explains_results_without_cli_codes(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            self.app._source_path = source
            self.app._source_is_file = True
            self.app._previews[str(source)] = PdfPreview(
                source, 100, 0, 0.0, True, "待添加书签")
            report = Path(directory) / "bookmarker-report.jsonl"
            self.app._run_report = gui.RunReport(report)
            self.app._handle_worker_line(f"正在处理 PDF：{source}")
            self.app._handle_worker_line("第 1/1 本：book.pdf；正在处理")
            report.write_text(json.dumps({
                "source": str(source), "status": "success",
                "entries": [{} for _ in range(111)],
                "toc_pages": [21, 22, 23, 24, 25, 26],
                "api_usage": {"prompt_tokens": 100, "completion_tokens": 50,
                              "total_tokens": 150, "reported_responses": 1},
            }) + "\n", encoding="utf-8")
            self.app._handle_worker_line(
                "  success: 111 entries, TOC [21, 22, 23, 24, 25, 26]")
            self.app._handle_worker_line("Summary: success=1")
            self.app._handle_worker_line("处理结果：已生成书签 PDF，写入 111 条书签")
            self.app._handle_worker_line("本次已统计 API Token：输入 100，输出 50，合计 150")
            self.app._drain_run_report()
            log = self.app.log.get("1.0", "end")
            self.assertIn("已写入 111 条书签", log)
            self.assertIn("印刷目录位于 PDF 第 21—26 页", log)
            self.assertNotIn("TOC", log)
            self.assertNotIn("entries", log)
            self.assertNotIn("success:", log)
            self.assertNotIn(str(source), log)

    def test_book_progress_is_a_distinct_copyable_log_heading(self) -> None:
        self._skip_initial_source_load()
        self.app._append("找到 20 本 PDF，开始逐本检查。")
        self.app._handle_worker_line("第 1/20 本：一本文件名很长的书.pdf；正在处理")
        self.app._handle_worker_line("[2/20] resume skip: second.pdf")

        log = self.app.log.get("1.0", "end")
        self.assertIn("\n第 1/20 本\n一本文件名很长的书.pdf；正在处理。\n", log)
        self.assertIn("\n第 2/20 本\nsecond.pdf；续跑记录显示已完成，本次跳过。\n", log)
        spans = self.app.log.tag_ranges("book_progress")
        self.assertEqual(len(spans), 4)
        self.assertEqual(self.app.log.get(spans[0], spans[1]).strip(), "第 1/20 本")
        self.assertEqual(self.app.log.get(spans[2], spans[3]).strip(), "第 2/20 本")
        self.assertEqual(self.app.log.tag_cget("book_progress", "foreground"), "#174A82")
        self.assertIn(gui.font()[0], self.app.log.cget("font"))
        self.assertIn(gui.font()[0], self.app.log.tag_cget("book_progress", "font"))

    def test_failed_pdf_remains_red_when_selected(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("ok.pdf", "failed.pdf")]
            self.app._source_path = Path(directory)
            self.app._previews = {
                str(path): PdfPreview(path, 12, 0, 0.0, True, "待添加书签")
                for path in paths
            }
            self.app._file_indices = {str(path): index for index, path in
                                      enumerate(paths, 1)}
            self.app._run_status = {str(paths[0]): "success", str(paths[1]): "failed"}
            with patch.object(self.app, "_show_preview"):
                self.app._rebuild_list()
                self.app.pdf_tree.selection_set(str(paths[1]))
                self.app._on_pdf_selected()
                style = self.app._style
                self.assertIn("failed", self.app.pdf_tree.item(str(paths[1]), "tags"))
                self.assertEqual(self.app.pdf_tree.tag_configure("failed")["foreground"],
                                 gui.COLORS.failure)
                self.assertEqual(self.app.pdf_tree.tag_configure("failed")["background"],
                                 gui.COLORS.failure_pale)
                self.assertEqual(style.lookup("PdfList.Card.Treeview", "foreground",
                                              ("selected",)), gui.COLORS.failure)
                self.assertEqual(style.lookup("PdfList.Card.Treeview", "background",
                                              ("selected",)), gui.COLORS.failure_selection)
                self.assertEqual(style.lookup("Card.Treeview", "background",
                                              ("selected",)), gui.COLORS.blue_selection)
                self.app.pdf_tree.selection_set(str(paths[0]))
                self.app._on_pdf_selected()
                self.assertEqual(style.lookup("PdfList.Card.Treeview", "background",
                                              ("selected",)), gui.COLORS.blue_selection)

    def test_failed_log_result_and_book_heading_are_red(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            self.app._source_path = source
            self.app._source_is_file = True
            self.app._previews[str(source)] = PdfPreview(
                source, 438, 2, 0.0, True, "待添加书签")
            report = Path(directory) / "bookmarker-report.jsonl"
            self.app._run_report = gui.RunReport(report)
            self.app._handle_worker_line("第 4/24 本：book.pdf；正在处理")
            report.write_text(json.dumps({
                "source": str(source), "status": "failed", "entries": [],
                "error": "PdfminerException: No /Root object!",
            }) + "\n", encoding="utf-8")
            self.app._drain_run_report()
            heading = self.app.log.tag_ranges("failed_progress")
            result = self.app.log.tag_ranges("failed_result")
            self.assertEqual(self.app.log.get(heading[0], heading[1]).strip(),
                             "第 4/24 本")
            self.assertIn("处理失败", self.app.log.get(result[0], result[1]))
            self.assertEqual(self.app.log.tag_cget("failed_progress", "foreground"),
                             gui.COLORS.failure)
            self.assertEqual(self.app.log.tag_cget("failed_result", "background"),
                             gui.COLORS.failure_pale)

    def test_unrecognized_worker_warning_does_not_hide_reported_failure(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            self.app._source_path = source
            self.app._source_is_file = True
            self.app._previews[str(source)] = PdfPreview(
                source, 100, 0, 0.0, True, "待添加书签")
            report = Path(directory) / "bookmarker-report.jsonl"
            self.app._run_report = gui.RunReport(report)
            self.app._handle_worker_line("第 1/1 本：book.pdf；正在处理")
            self.app._handle_worker_line("library warning: a PDF object was repaired")
            report.write_text(json.dumps({
                "source": str(source), "status": "failed", "entries": [],
                "toc_pages": [6, 7], "error": "无法写入此 PDF，请检查磁盘空间",
            }) + "\n", encoding="utf-8")
            self.app._drain_run_report()
            log = self.app.log.get("1.0", "end")
            self.assertIn("处理失败，原 PDF 未改动", log)
            self.assertIn("无法写入此 PDF，请检查磁盘空间", log)
            self.assertNotIn("额外信息", log)
            self.assertNotIn("library warning", log)

    def test_worker_exit_without_result_explains_premature_end(self) -> None:
        self._skip_initial_source_load()
        self.app._handle_worker_line("unexpected diagnostic from PDF library")
        self.app._events.put(("done", 1))
        self._until(lambda: "处理程序提前结束" in self.app.log.get("1.0", "end"))
        log = self.app.log.get("1.0", "end")
        self.assertIn("未取得文件结果", log)
        self.assertNotIn("额外信息", log)
        self.assertNotIn("unexpected diagnostic", log)

    def test_start_gives_each_run_a_unique_stop_flag_inside_job_data(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            source.touch()
            output = Path(directory) / "output"
            job_dir = Path(directory) / "data" / "jobs" / "this-job"
            with patch.object(gui, "inspect_pdf", return_value=PdfPreview(
                    source, 3, 0, 0.0, True, "待添加书签")), \
                 patch.object(gui, "job_data_dir", return_value=job_dir):
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(output))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning)
                self.app.api_key_var.set("test-key")
                with patch.object(gui.subprocess, "Popen", return_value=Mock()) as popen, \
                     patch.object(gui.threading, "Thread"):
                    self.app._start()
                    first_command = popen.call_args.args[0]
                    first = Path(first_command[first_command.index("--stop-file") + 1])
                    self.app._process = None
                    self.app._set_running(False)
                    self.app._start()
                    second_command = popen.call_args.args[0]
                    second = Path(second_command[second_command.index("--stop-file") + 1])
                    self.app._process = None
                    self.app._set_running(False)
            self.assertEqual(first.parent, job_dir)
            self.assertEqual(second.parent, job_dir)
            self.assertNotEqual(first, second)

    def test_stop_waits_for_current_pdf_and_cleans_flag_after_done(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            flag = Path(directory) / "data" / "jobs" / "job" / "stop-test.flag"
            process = Mock()
            process.poll.return_value = None
            self.app._process = process
            self.app._stop_file = flag
            self.app._set_running(True)
            self.assertTrue(self.app._stop())
            self.assertTrue(flag.is_file())
            self.assertTrue(self.app._running)
            self.assertIn("当前 PDF", self.app.status_var.get())
            process.terminate.assert_not_called()
            self.app._events.put(("done", 130))
            self._until(lambda: not self.app._running)
            self.assertFalse(flag.exists())
            self.assertIsNone(self.app._stop_file)
            self.assertIn("已停止", self.app.status_var.get())

    def test_close_during_processing_waits_until_worker_done(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            process = Mock()
            process.poll.return_value = None
            self.app._process = process
            self.app._stop_file = Path(directory) / "data" / "jobs" / "job" / "stop.flag"
            self.app._set_running(True)
            with patch.object(gui.messagebox, "askyesno", return_value=True):
                self.app._close()
            self.assertTrue(self.root.winfo_exists())
            self.assertTrue(self.app._close_when_done)
            self.assertTrue(self.app._stop_file.is_file())
            process.terminate.assert_not_called()
            self.app._events.put(("done", 130))
            self.root.after_cancel(self.app._poll_after)
            self.app._poll_after = None
            with patch.object(self.app, "_finish_close") as finish_close:
                self.app._poll_events()
                finish_close.assert_called_once()
            self.assertTrue(self.root.winfo_exists())

    def test_main_recovers_interrupted_overwrite_before_opening_window(self) -> None:
        recovered = Path("C:/Books/recovered.pdf")
        root = MagicMock()
        app = MagicMock()
        order = []
        with patch.object(gui.tk, "Tk", return_value=root), \
             patch.object(gui, "recover_pending_overwrites",
                          side_effect=lambda: order.append("recover") or [recovered]), \
             patch.object(gui, "BookmarkApp",
                          side_effect=lambda _root: order.append("app") or app), \
             patch.object(gui.messagebox, "showinfo") as showinfo:
            gui.main()
        self.assertEqual(order, ["recover", "app"])
        root.withdraw.assert_called_once()
        root.deiconify.assert_called_once()
        app._append.assert_any_call(f"已恢复：{recovered}")
        showinfo.assert_called_once()
        root.mainloop.assert_called_once()

    def test_main_does_not_open_window_when_recovery_needs_attention(self) -> None:
        root = MagicMock()
        with patch.object(gui.tk, "Tk", return_value=root), \
             patch.object(gui, "recover_pending_overwrites",
                          side_effect=RuntimeError("备份校验失败")), \
             patch.object(gui, "BookmarkApp") as app, \
             patch.object(gui.messagebox, "showerror") as showerror:
            gui.main()
        app.assert_not_called()
        self.assertIn("备份校验失败", showerror.call_args.args[1])
        root.destroy.assert_called_once()
        root.mainloop.assert_not_called()

    def test_token_usage_shows_totals_and_missing_data(self) -> None:
        self._skip_initial_source_load()
        self.app._run_results = {
            "a.pdf": {"status": "success", "api_usage": {
                "prompt_tokens": 1234, "completion_tokens": 56,
                "total_tokens": 1290, "reported_responses": 2,
                "unreported_responses": 1}},
            "b.pdf": {"status": "failed"},
        }
        self.app._update_token_usage(completed=True)
        usage = self.app.token_usage_var.get()
        self.assertIn("输入 1,234", usage)
        self.assertIn("输出 56", usage)
        self.assertIn("合计 1,290", usage)
        self.assertIn("1 次响应未提供用量", usage)
        self.assertIn("1 本缺少用量记录", usage)
        self.assertEqual(self.app.token_usage_label.winfo_manager(), "grid")
        self.app._run_results = {}
        self.app._update_token_usage(completed=True)
        self.assertIn("未收到 API Token 用量记录", self.app.token_usage_var.get())

    def test_policy_switch_keeps_preview_and_does_not_rescan_pdf(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "marked.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=300)
            writer.add_outline_item("Chapter One", 0)
            with source.open("wb") as stream:
                writer.write(stream)
            with patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60))):
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning and self.app._cover_image is not None)
                generation = self.app._scan_generation
                image = self.app._cover_image
                with patch.object(gui, "inspect_pdf", side_effect=AssertionError("PDF reopened")):
                    self.app.policy_var.set("skip")
                    self.app._on_policy_changed()
                self.assertEqual(self.app._scan_generation, generation)
                self.assertIs(self.app._cover_image, image)
                self.assertEqual(self.app._displayed_path, source)
                self.assertEqual(self.app.pdf_tree.set(str(source), "action"), "跳过")
                self.assertEqual(self.app.start_button["state"], "disabled")

    def test_result_panel_shows_review_and_bookmark_navigation(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            writer = PdfWriter()
            for _ in range(3):
                writer.add_blank_page(width=200, height=300)
            with source.open("wb") as stream:
                writer.write(stream)
            with patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60))), \
                 patch.object(gui, "render_page", return_value=Image.new("RGB", (40, 60))):
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning and self.app._cover_image is not None)
                report = Path(directory) / "output" / "bookmarker-report.jsonl"
                self.app._run_report = gui.RunReport(report)
                report.parent.mkdir()
                report.write_text(json.dumps({
                    "source": str(source), "status": "needs_review", "toc_pages": [3],
                    "entries": [{"title": "Chapter One", "level": 1, "pdf_page": 2}],
                    "warnings": ["页码映射不可靠"], "output": None,
                }) + "\n", encoding="utf-8")
                self.app._drain_run_report()
                self.assertEqual(self.app.pdf_tree.set(str(source), "action"), "需复核")
                self.assertIn("页码映射不可靠", self.app.result_detail_var.get())
                item = self.app.result_tree.get_children()[0]
                self.app.result_tree.selection_set(item)
                self.app._on_result_selected()
                self.assertEqual(self.app._page_number, 2)
                self.app._jump_to_toc()
                self.assertEqual(self.app._page_number, 3)
                self.app.filter_var.set("需复核")
                self.assertEqual(self.app.pdf_tree.get_children(), (str(source),))

    def test_result_panel_lists_contents_page_bookmark_first(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            writer = PdfWriter()
            for _ in range(3):
                writer.add_blank_page(width=200, height=300)
            with source.open("wb") as stream:
                writer.write(stream)
            with patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60))), \
                 patch.object(gui, "render_page", return_value=Image.new("RGB", (40, 60))):
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning and self.app._cover_image is not None)
                report = Path(directory) / "output" / "bookmarker-report.jsonl"
                self.app._run_report = gui.RunReport(report)
                report.parent.mkdir()
                row = {
                    "source": str(source), "status": "success", "toc_pages": [2],
                    "toc_bookmark": {"title": "目录", "level": 1, "pdf_page": 2},
                    "entries": [{"title": "第1章", "level": 1, "pdf_page": 3}],
                    "warnings": [], "output": None,
                }
                report.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
                self.app._drain_run_report()
                items = self.app.result_tree.get_children()
                self.assertEqual([self.app.result_tree.set(item, "title") for item in items],
                                 ["目录", "第1章"])
                self.assertEqual(self.app.result_tree.set(items[0], "page"), "2")
                self.assertIn("识别 1 条", self.app.result_count_var.get())
                self.assertIn("已写入 2 条书签", self.app._result_log_message(row))

    def test_review_result_can_be_confirmed_without_losing_other_results(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            books = root / "books"
            books.mkdir()
            for name in ("a.pdf", "b.pdf"):
                writer = PdfWriter()
                for _ in range(3):
                    writer.add_blank_page(width=200, height=300)
                with (books / name).open("wb") as stream:
                    writer.write(stream)
            held, done = books / "a.pdf", books / "b.pdf"
            job_dir = root / "data" / "jobs" / "job"
            job_dir.mkdir(parents=True)
            report = job_dir / "bookmarker-report.jsonl"

            def row(path: Path, status: str, **extra: object) -> dict:
                stat = path.stat()
                return {"source": str(path), "status": status, "toc_pages": [2],
                        "entries": [{"title": "第1章", "level": 1, "pdf_page": 3}],
                        "warnings": ["目录页不连续，需要人工确认是否漏页"], "output": None,
                        "source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
                        **extra}

            with patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60))), \
                 patch.object(gui, "render_page", return_value=Image.new("RGB", (40, 60))), \
                 patch.object(gui, "job_data_dir", return_value=job_dir):
                self.app.input_var.set(str(books))
                self.app.output_var.set(str(root / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning and self.app._cover_image is not None)
                self.app._report_path = job_dir / gui.REPORT_NAME
                self.app._run_report = gui.RunReport(report)
                report.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in (
                    row(held, "needs_review", review_acceptable=True),
                    row(done, "success"))) + "\n", encoding="utf-8")
                self.app._drain_run_report()
                self.app.pdf_tree.selection_set(str(held))
                self.app._on_pdf_selected()
                self._until(lambda: self.app._displayed_path == held)
                self.assertEqual(self.app.accept_review_button.winfo_manager(), "pack")
                self.assertEqual(str(self.app.accept_review_button["state"]), "normal")

                self.app.api_key_var.set("test-key")
                with patch.object(gui.messagebox, "askyesno", return_value=True) as ask, \
                     patch.object(gui.subprocess, "Popen", return_value=Mock()) as popen, \
                     patch.object(gui.threading, "Thread"):
                    self.app._accept_review()
                command = popen.call_args.args[0]
                self.assertIn("写入 1 条书签", ask.call_args.args[1])
                self.assertEqual(command[command.index("--accept-review") + 1], str(held))
                self.assertNotIn("--resume", command)
                self.assertNotIn("--dry-run", command)
                self.assertTrue(self.app._running)
                self.assertEqual(str(self.app.accept_review_button["state"]), "disabled")

                with report.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row(held, "success", review_accepted=True),
                                            ensure_ascii=False) + "\n")
                self.app._events.put(("done", 0))
                self._until(lambda: not self.app._running)
            self.assertEqual(self.app._run_results[str(held)]["status"], "success")
            self.assertEqual(self.app._run_results[str(done)]["status"], "success")
            self.assertEqual(self.app.status_var.get(), "已按确认写入书签")
            self.assertIn("已按人工确认写入", self.app.result_detail_var.get())
            self.assertEqual(self.app.accept_review_button.winfo_manager(), "")
            self.assertEqual(self.app.pdf_tree.set(str(held), "action"), "已完成")

    def test_review_without_target_pages_cannot_be_confirmed(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=300)
            with source.open("wb") as stream:
                writer.write(stream)
            with patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60))), \
                 patch.object(gui, "render_page", return_value=Image.new("RGB", (40, 60))):
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning and self.app._cover_image is not None)
                report = Path(directory) / "output" / "bookmarker-report.jsonl"
                self.app._run_report = gui.RunReport(report)
                report.parent.mkdir()
                report.write_text(json.dumps({
                    "source": str(source), "status": "needs_review", "toc_pages": [1],
                    "entries": [{"title": "第1章", "level": 1, "pdf_page": None}],
                    "warnings": ["1 条目录条目无法对应 PDF 页"], "review_acceptable": False,
                }, ensure_ascii=False) + "\n", encoding="utf-8")
                self.app._drain_run_report()
            self.assertEqual(self.app.accept_review_button.winfo_manager(), "")
            self.assertIn("不能确认写入", self.app.result_detail_var.get())

    def test_fast_worker_result_is_seen_as_this_run(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=300)
            with source.open("wb") as stream:
                writer.write(stream)
            output = root / "output"
            with patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60))):
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(output))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning)
            self.app.api_key_var.set("test-key")
            job_dir = gui.job_data_dir(source, output, root=root / "data")

            def complete_immediately(*_args, **_kwargs):
                job_dir.mkdir(parents=True)
                (job_dir / "bookmarker-report.jsonl").write_text(json.dumps({
                    "source": str(source), "status": "needs_review", "entries": [],
                    "warnings": ["需人工复核"],
                }) + "\n", encoding="utf-8")
                (job_dir / "bookmarker-summary.csv").write_text(
                    "source,status\n", encoding="utf-8")
                class Process:
                    stdout = io.StringIO("")
                    def wait(self):
                        return 0
                return Process()

            with patch.object(gui.subprocess, "Popen", side_effect=complete_immediately), \
                 patch.object(gui, "job_data_dir", return_value=job_dir):
                self.app._start()
                self._until(lambda: not self.app._running)
            self.assertEqual(self.app._run_results[str(source)]["status"], "needs_review")
            self.assertIn("需复核", self.app.status_var.get())
            self.assertEqual(self.app.report_button["state"], "normal")

    def test_scan_without_resume_ignores_existing_report(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            source.touch()
            with patch.object(gui, "_done_files", side_effect=AssertionError("old report read")), \
                 patch.object(gui, "inspect_pdf", return_value=PdfPreview(
                     source, 3, 0, 0.0, True, "待添加书签")):
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning)
            self.assertEqual(self.app.pdf_tree.get_children(), (str(source),))

    def test_resume_switch_reads_report_without_rescanning_pdf(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            source.touch()
            with patch.object(gui, "inspect_pdf", return_value=PdfPreview(
                    source, 3, 0, 0.0, True, "待添加书签")) as inspect, \
                 patch.object(gui, "_done_files", return_value={str(source): {"status": "success"}}), \
                 patch.object(gui, "_resume_skips", return_value=True):
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning)
                generation = self.app._scan_generation
                self.app.resume_var.set(True)
                self._until(lambda: self.app._resume_done_loaded and
                            self.app.pdf_tree.set(str(source), "action") == "续跑跳过")
            self.assertEqual(self.app._scan_generation, generation)
            self.assertEqual(inspect.call_count, 1)

    def test_unreadable_resume_report_can_be_bypassed_without_rescan(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            source.touch()
            with patch.object(gui, "inspect_pdf", return_value=PdfPreview(
                    source, 3, 0, 0.0, True, "待添加书签")) as inspect, \
                 patch.object(gui, "_done_files", side_effect=UnicodeError("bad report")):
                self.app.resume_var.set(True)
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning)
                self.assertIn("续跑记录无法读取", self.app.status_var.get())
                self.assertEqual(self.app.start_button["state"], "disabled")
                generation = self.app._scan_generation
                self.app.resume_var.set(False)
                self.assertEqual(self.app.start_button["state"], "normal")
                self.assertEqual(self.app._scan_generation, generation)
                self.assertEqual(inspect.call_count, 1)

    def test_progress_uses_file_path_and_keeps_completed_status(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            a, b = folder / "a.pdf", folder / "b.pdf"
            self.app._source_path = folder
            self.app._previews = {str(path): PdfPreview(path, 1, 0, 0.0, True,
                                   "待添加书签") for path in (a, b)}
            self.app._file_indices = {str(a): 1, str(b): 2}
            self.app._run_status = {str(a): "success", str(b): "queued"}
            self.app._handle_worker_line("[1/3] b.pdf")
            self.assertEqual(self.app._run_status[str(a)], "success")
            self.assertEqual(self.app._run_status[str(b)], "running")
            self.app._handle_worker_line("[2/3] a.pdf")
            self.assertEqual(self.app._run_status[str(a)], "success")

    def test_replaced_report_removes_stale_run_result(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "bookmarker-report.jsonl"
            self.app._run_report = gui.RunReport(report)
            first = {"source": "C:/old.pdf", "status": "failed"}
            second = {"source": "C:/new.pdf", "status": "success"}
            report.write_text(json.dumps(first) + "\n", encoding="utf-8")
            self.app._drain_run_report()
            self.assertIn(first["source"], self.app._run_results)
            report.write_text(json.dumps(second) + "\n", encoding="utf-8")
            self.app._drain_run_report()
            self.assertEqual(set(self.app._run_results), {second["source"]})
            self.assertNotIn(first["source"], self.app._run_status)

    def test_completion_prioritizes_failures_and_marks_unfinished(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("failed.pdf", "review.pdf", "pending.pdf")]
            self.app._previews = {str(path): PdfPreview(path, 1, 0, 0.0, True,
                                   "待添加书签") for path in paths}
            self.app._file_indices = {str(path): index for index, path in enumerate(paths, 1)}
            self.app._run_report = SimpleNamespace(status_counts={"failed": 1,
                                                                    "needs_review": 1})
            self.app._run_status = {str(paths[0]): "failed", str(paths[1]): "needs_review",
                                    str(paths[2]): "queued"}
            self.app._update_run_summary(finished=True, exit_code=1)
            self.assertEqual(self.app.filter_var.get(), "失败")
            self.assertEqual(self.app._run_status[str(paths[2])], "unprocessed")
            self.assertIn("1 本失败", self.app.status_var.get())

    def test_run_scope_change_triggers_new_scan_before_start(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "books"
            folder.mkdir()
            first = folder / "a.pdf"
            first.touch()
            with patch.object(gui, "inspect_pdf", side_effect=lambda path, **_kwargs:
                    PdfPreview(Path(path), 3, 0, 0.0, True, "待添加书签")), \
                 patch.object(gui.subprocess, "Popen") as popen:
                self.app.input_var.set(str(folder))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning)
                (folder / "b.pdf").touch()
                self.app._start()
                self._until(lambda: not self.app._scanning and
                            len(self.app.pdf_tree.get_children()) == 2)
            popen.assert_not_called()
            self.assertIn("来源文件已变化", self.app.status_var.get())

    def test_unreadable_run_report_shows_start_error(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            source.touch()
            output = Path(directory) / "output"
            job_dir = gui.job_data_dir(source, output, root=Path(directory) / "data")
            (job_dir / "bookmarker-report.jsonl").mkdir(parents=True)
            with patch.object(gui, "inspect_pdf", return_value=PdfPreview(
                    source, 3, 0, 0.0, True, "待添加书签")), \
                 patch.object(gui.messagebox, "showerror") as showerror, \
                 patch.object(gui.subprocess, "Popen") as popen, \
                 patch.object(gui, "job_data_dir", return_value=job_dir):
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(output))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning)
                self.app.api_key_var.set("test-key")
                self.app._start()
            popen.assert_not_called()
            showerror.assert_called_once()
            self.assertIn("启动失败", showerror.call_args.args[0])

    def test_run_summary_uses_workers_actual_file_count(self) -> None:
        self._skip_initial_source_load()
        self.app._worker_total = 3
        self.app._run_report = SimpleNamespace(status_counts={"success": 3})
        self.app._update_run_summary()
        self.assertIn("已检查 3/3", self.app.run_summary_var.get())

    def test_refresh_after_overwrite_keeps_completion_message(self) -> None:
        self._skip_initial_source_load()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.pdf"
            source.touch()
            with patch.object(gui, "inspect_pdf", return_value=PdfPreview(
                    source, 3, 0, 0.0, True, "待添加书签")):
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning)
                self.app.status_var.set("处理结束；1 本需复核")
                self.app.run_summary_var.set("已检查 1/1  ·  需复核 1")
                self.app._load_source(force=True, keep_results=True)
                self._until(lambda: not self.app._scanning)
            self.assertEqual(self.app.status_var.get(), "处理结束；1 本需复核")
            self.assertIn("已检查 1/1", self.app.run_summary_var.get())

    def test_settings_popup_keeps_workspace_usable_at_minimum_window_size(self) -> None:
        self._skip_initial_source_load()
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.root.geometry("1120x700")
        self.root.update()
        collapsed_cover_height = self.app.cover_canvas.winfo_height()
        collapsed_list_height = self.app.pdf_tree.winfo_height()

        self.app._toggle_settings()
        self.root.update()
        self.assertTrue(self.app.settings_window.winfo_ismapped())
        self.assertEqual(self.root.winfo_height(), 700)
        self.assertGreaterEqual(self.app.cover_canvas.winfo_height(), 180)
        self.assertEqual(self.app.cover_canvas.winfo_height(), collapsed_cover_height)
        self.assertEqual(self.app.pdf_tree.winfo_height(), collapsed_list_height)
        self.assertLessEqual(self.app.settings_window.winfo_rooty() +
                             self.app.settings_window.winfo_height(),
                             self.root.winfo_screenheight())

        self.app._toggle_settings()
        self.root.update()
        self.assertFalse(self.app.settings_window.winfo_ismapped())

    def test_narrowest_panes_keep_visible_table_columns(self) -> None:
        self._skip_initial_source_load()
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.root.geometry("1120x700")
        self.root.update()
        pane = self.app.pdf_tree.master.master.master.master

        pane.sash_place(0, 330, 0)
        self.root.update()
        visible_pdf_columns = ("#0", "file", "pages", "action")
        self.assertGreaterEqual(
            self.app.pdf_tree.winfo_width(),
            sum(self.app.pdf_tree.column(column, "minwidth")
                for column in visible_pdf_columns))

        pane.sash_place(1, pane.winfo_width() - 260, 0)
        self.root.update()
        self.assertGreaterEqual(
            self.app.outline_tree.winfo_width(),
            sum(self.app.outline_tree.column(column, "minwidth")
                for column in ("#0", "title", "page")))
        self.app.detail_tabs.select(1)
        self.root.update()
        self.assertGreaterEqual(
            self.app.result_tree.winfo_width(),
            sum(self.app.result_tree.column(column, "minwidth")
                for column in ("#0", "title", "page")))

    def test_cached_api_key_is_loaded_masked_on_next_window(self) -> None:
        with patch.object(gui, "load_key", return_value="sk-cached-for-test"):
            another_root = tk.Tk()
            another_root.withdraw()
            another = gui.BookmarkApp(another_root)
            try:
                self.assertEqual(another.api_key_var.get(), "sk-cached-for-test")
                self.assertEqual(another.api_key_entry.cget("show"), "*")
            finally:
                another._close()


if __name__ == "__main__":
    unittest.main()
