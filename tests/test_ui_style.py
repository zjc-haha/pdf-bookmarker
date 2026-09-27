"""The workbench uses one font family and bundled, recolorable UI icons."""

from __future__ import annotations

import tkinter as tk
import unittest
from pathlib import Path
from tkinter import font as tkfont
from types import SimpleNamespace

from PIL import Image, ImageTk

from bookmarker.ui_style import (
    COLORS, FONT_FAMILY, RoundedButton, apply_theme, font, render_icon,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ICON_NAMES = (
    "pdf", "folder", "eye", "bookmark", "settings", "search", "play",
    "stop", "info", "list", "report", "left", "right", "plus", "minus",
    "expand", "close", "check", "key", "folder-open", "file-report", "file-text",
    "zoom-in", "zoom-out",
)


class IconAssetTest(unittest.TestCase):
    def test_all_ui_icons_have_local_high_resolution_sources(self) -> None:
        icon_dir = PROJECT_ROOT / "bookmarker" / "assets" / "icons"
        for name in ICON_NAMES:
            with self.subTest(name=name):
                source = icon_dir / f"{name}.png"
                self.assertTrue(source.is_file(), f"missing icon source: {source}")
                with Image.open(source) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.mode, "RGBA")
                    self.assertEqual(image.size, (96, 96))
                    alpha = image.getchannel("A")
                    self.assertEqual(alpha.getextrema(), (0, 255))
                    self.assertIsNotNone(alpha.getbbox())

    def test_rendered_icons_are_sized_transparent_and_recolorable(self) -> None:
        for name in ICON_NAMES:
            with self.subTest(name=name):
                blue = render_icon(name, size=16, color="#1767EA")
                disabled = render_icon(name, size=16, color="#A7B6CD")
                self.assertEqual(blue.mode, "RGBA")
                self.assertEqual(blue.size, (16, 16))
                self.assertEqual(disabled.size, (16, 16))
                self.assertEqual(blue.getchannel("A").tobytes(),
                                 disabled.getchannel("A").tobytes())
                self.assertEqual(blue.getpixel((0, 0))[3], 0)
                self.assertIsNotNone(blue.getchannel("A").getbbox())
                self.assertNotEqual(blue.tobytes(), disabled.tobytes())

        self.assertEqual(render_icon("folder-open", size=36).size, (36, 36))
        with self.assertRaises(ValueError):
            render_icon("not-an-icon")
        with self.assertRaises(ValueError):
            render_icon("pdf", size=0)

    def test_portable_bundle_includes_icon_license(self) -> None:
        packaging = PROJECT_ROOT / "packaging"
        filename = "Tabler-Icons-LICENSE.txt"
        license_path = packaging / "licenses" / filename
        self.assertTrue(license_path.is_file())
        license_text = license_path.read_text(encoding="utf-8").lower()
        self.assertIn("permission is hereby granted", license_text)
        self.assertIn("copyright", license_text)

        portable_script = (packaging / "package_portable.ps1").read_text(encoding="utf-8")
        notices = (packaging / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn(f'"{filename}"', portable_script)
        self.assertIn(filename, notices)
        self.assertIn("Tabler Icons", notices)

    def test_pyinstaller_collects_every_icon_for_offline_use(self) -> None:
        spec = PROJECT_ROOT / "packaging" / "PDFBookmarker.spec"
        bundled_data: list[tuple[str, str]] = []

        def fake_analysis(*_args: object, **kwargs: object) -> SimpleNamespace:
            bundled_data.extend(kwargs["datas"])
            return SimpleNamespace(pure=[], scripts=[], binaries=[], zipfiles=[], datas=[])

        # A spec file is Python. Evaluate it with build steps stubbed so the
        # test checks resolved bundle paths without running PyInstaller.
        namespace = {
            "SPECPATH": str(spec.parent),
            "Analysis": fake_analysis,
            "PYZ": lambda *_args, **_kwargs: object(),
            "EXE": lambda *_args, **_kwargs: object(),
            "COLLECT": lambda *_args, **_kwargs: None,
        }
        exec(compile(spec.read_text(encoding="utf-8"), str(spec), "exec"), namespace)

        expected = {
            (PROJECT_ROOT / "bookmarker" / "assets" / "icons" / f"{name}.png").resolve()
            for name in ICON_NAMES
        }
        actual = {
            Path(source).resolve()
            for source, destination in bundled_data
            if destination == "bookmarker/assets/icons"
        }
        self.assertEqual(actual, expected)


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

    def test_button_icon_tracks_enabled_and_disabled_foreground(self) -> None:
        button = RoundedButton(self.root, text="开始", kind="primary", icon_name="play")
        try:
            self.assertIsNotNone(button._icon_image)
            enabled = ImageTk.getimage(button._icon_image)
            self.assertIn((255, 255, 255, 255),
                          {pixel for _, pixel in enabled.getcolors(maxcolors=257)})

            button.configure(state="disabled")
            self.assertIsNotNone(button._icon_image)
            disabled = ImageTk.getimage(button._icon_image)
            muted = tuple(bytes.fromhex(COLORS.disabled.lstrip("#")))
            self.assertIn((*muted, 255),
                          {pixel for _, pixel in disabled.getcolors(maxcolors=257)})
        finally:
            button.destroy()


if __name__ == "__main__":
    unittest.main()
