"""The workbench uses one font family in Tk and ttk controls."""

from __future__ import annotations

import tkinter as tk
import unittest
from tkinter import font as tkfont

from bookmarker.ui_style import FONT_FAMILY, RoundedButton, apply_theme, font


class ThemeFontTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.root.withdraw()

    def tearDown(self) -> None:
        if hasattr(self, "root"):
            self.root.destroy()

    def test_named_fonts_and_widgets_use_microsoft_yahei(self) -> None:
        style = apply_theme(self.root)
        self.assertEqual(FONT_FAMILY, "Microsoft YaHei")
        self.assertEqual(font(11, bold=True), (FONT_FAMILY, 11, "bold"))

        for name in ("TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont",
                     "TkHeadingFont"):
            with self.subTest(name=name):
                self.assertEqual(tkfont.nametofont(name, root=self.root).cget("family"),
                                 FONT_FAMILY)

        for widget in (tk.Label(self.root, text="普通文字"),
                       tk.Text(self.root),
                       tk.Listbox(self.root)):
            with self.subTest(widget=widget.winfo_class()):
                self.assertEqual(self.root.tk.splitlist(widget.cget("font"))[0],
                                 FONT_FAMILY)
            widget.destroy()

        for style_name in (".", "App.TCombobox", "Card.Treeview",
                           "Card.Treeview.Heading", "App.TCheckbutton"):
            with self.subTest(style=style_name):
                style_font = style.lookup(style_name, "font")
                self.assertEqual(self.root.tk.splitlist(style_font)[0], FONT_FAMILY)

        button = RoundedButton(self.root, text="处理")
        self.assertEqual(button._font.cget("family"), FONT_FAMILY)
        button.destroy()


if __name__ == "__main__":
    unittest.main()
