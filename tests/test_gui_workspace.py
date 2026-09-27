"""Interactive workspace checks without starting a recognition worker."""

from __future__ import annotations

import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_skip_bookmarked_lists_only_pdfs_without_bookmarks(self) -> None:
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
                            self.app.pdf_tree.get_children() == (str(empty),))
                self.assertEqual(self.app._scan_skipped, 1)

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
                            len(self.app.pdf_tree.get_children()) == 2)

                names = {Path(item).name for item in self.app.pdf_tree.get_children()}
                self.assertEqual(names, {"a.pdf", "b.pdf"})
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
                self.assertEqual(self.app.outline_action_var.get(), "待添加书签")
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
                eligible = path.name != "b.pdf"
                return PdfPreview(path, 12, 0, 0.0, eligible,
                                  "待添加书签" if eligible else "已有完整书签")

            with patch.object(gui, "inspect_pdf", side_effect=inspect), \
                 patch.object(gui, "render_first_page", return_value=Image.new("RGB", (40, 60))):
                self.app.verify_var.set(False)
                self.app.input_var.set(str(source))
                self.app.output_var.set(str(Path(directory) / "output"))
                self.app._load_source(force=True)
                self._until(lambda: not self.app._scanning and
                            len(self.app.pdf_tree.get_children()) == 2)

                a, c = (str(source / name) for name in ("a.pdf", "c.pdf"))
                self.assertEqual(self.app.pdf_tree.get_children(), (a, c))
                self.assertEqual(self.app.pdf_tree.heading("#0", "text"), "序号")
                self.assertEqual(self.app.pdf_tree.item(a, "text"), "1")
                self.assertEqual(self.app.pdf_tree.item(c, "text"), "3")
                self.assertEqual(self.app.pdf_tree.set(c, "file"), "c.pdf")

                self.app.search_var.set("c.pdf")
                self.assertEqual(self.app.pdf_tree.get_children(), (c,))
                self.assertEqual(self.app.pdf_tree.item(c, "text"), "3")
                self.app.search_var.set("")
                self.assertEqual(self.app.pdf_tree.get_children(), (a, c))
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
        self.mock_save_key.assert_called_with("")

    def test_overwrite_checkbox_is_off_by_default_and_explains_report_location(self) -> None:
        self._skip_initial_source_load()
        self.assertFalse(self.app.overwrite_original_var.get())
        self.assertEqual(self.app.output_label.cget("text"), "输出")
        with patch.object(self.app, "_load_source") as reload_source:
            self.app.overwrite_original_var.set(True)
        reload_source.assert_called_once_with(force=True)
        self.assertIn("报告/缓存", self.app.output_label.cget("text"))
        self.assertTrue(self.app.overwrite_original_checkbox.instate(["selected"]))

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
