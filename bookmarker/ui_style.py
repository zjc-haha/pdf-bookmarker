"""Small visual building blocks for the desktop workbench.

The palette follows the supplied light-blue prototype.  Icons use local
Tabler outline assets and are tinted to match the widget state.  Keep returned
``PhotoImage`` objects referenced by the owning widget.
"""

from __future__ import annotations

import re
import tkinter as tk
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable, Literal

from PIL import Image, ImageDraw, ImageTk


@dataclass(frozen=True)
class Palette:
    background: str = "#F3F7FD"
    surface: str = "#FFFFFF"
    surface_tint: str = "#F8FAFE"
    preview: str = "#EAF1F8"
    text: str = "#12294C"
    muted: str = "#617697"
    border: str = "#DCE6F2"
    blue: str = "#1767EA"
    blue_hover: str = "#0F57D2"
    blue_pressed: str = "#0C49B1"
    blue_pale: str = "#EAF2FF"
    blue_selection: str = "#DCEAFF"
    failure: str = "#B42318"
    failure_pale: str = "#FFF0EF"
    failure_selection: str = "#FADBD8"
    disabled: str = "#A7B6CD"
    border_strong: str = "#B9C8DC"
    scroll_thumb: str = "#CFDAE8"
    scroll_thumb_active: str = "#A9BAD0"
    notice: str = "#EEF3FA"


COLORS = Palette()
FONT_FAMILY = "Microsoft YaHei"


def font(size: int = 10, *, bold: bool = False) -> tuple[str, int, str]:
    """Return a Tk font tuple matching the workbench typography."""
    return FONT_FAMILY, size, "bold" if bold else "normal"


def apply_theme(root: tk.Misc, colors: Palette = COLORS) -> ttk.Style:
    """Configure the standard ttk styles used by the PDF workbench.

    This uses the built-in ``clam`` theme so packaging needs no extra files.
    Canvas controls can use :class:`RoundedButton` for closer prototype edges.
    """
    # Tk's named fonts also supply text, listbox, menu and combobox popdown
    # defaults.  Changing only the ttk styles would leave those controls in
    # the operating system's default font.
    for name in tkfont.names(root):
        tkfont.nametofont(name, root=root).configure(family=FONT_FAMILY)
    root.option_add("*Font", f"{{{FONT_FAMILY}}} 10")

    style = ttk.Style(root)
    style.theme_use("clam")
    root.update_idletasks()
    style.configure(".", font=font(), foreground=colors.text, background=colors.surface,
                    focuscolor=colors.blue)
    flat = {"lightcolor": colors.surface, "darkcolor": colors.surface}
    style.configure(
        "App.TEntry", padding=(10, 7), fieldbackground=colors.surface,
        background=colors.surface, foreground=colors.text, insertcolor=colors.text,
        bordercolor=colors.border, relief="flat", selectbackground=colors.blue_selection,
        selectforeground=colors.text, **flat,
    )
    style.map("App.TEntry",
              bordercolor=[("focus", colors.blue), ("hover", colors.border_strong)],
              lightcolor=[("focus", colors.blue_pale)],
              foreground=[("disabled", colors.disabled)])
    # Borderless entry placed inside a bordered field frame with inline buttons.
    style.configure(
        "Field.TEntry", padding=(10, 7), fieldbackground=colors.surface,
        background=colors.surface, foreground=colors.text, insertcolor=colors.text,
        bordercolor=colors.surface, relief="flat", selectbackground=colors.blue_selection,
        selectforeground=colors.text, **flat,
    )
    style.map("Field.TEntry", foreground=[("disabled", colors.disabled)])
    style.configure(
        "App.TCombobox", padding=(9, 5), fieldbackground=colors.surface,
        background=colors.surface, foreground=colors.text, bordercolor=colors.border,
        arrowcolor=colors.muted, arrowsize=12, relief="flat",
        selectbackground=colors.surface, selectforeground=colors.text, **flat,
    )
    style.map(
        "App.TCombobox",
        fieldbackground=[("readonly", colors.surface)],
        background=[("readonly", colors.surface)],
        bordercolor=[("focus", colors.blue), ("hover", colors.border_strong)],
        arrowcolor=[("disabled", colors.disabled), ("hover", colors.blue)],
        selectbackground=[("readonly", colors.surface)],
        selectforeground=[("readonly", colors.text)],
        foreground=[("disabled", colors.disabled)],
    )
    root.option_add("*TCombobox*Listbox.background", colors.surface)
    root.option_add("*TCombobox*Listbox.foreground", colors.text)
    root.option_add("*TCombobox*Listbox.selectBackground", colors.blue_selection)
    root.option_add("*TCombobox*Listbox.selectForeground", colors.text)
    root.option_add("*TCombobox*Listbox.relief", "flat")
    style.configure(
        "App.TButton", padding=(12, 6), font=font(),
        background=colors.surface, foreground=colors.text,
        bordercolor=colors.border, relief="flat", **flat,
    )
    style.map(
        "App.TButton",
        background=[("pressed", colors.blue_selection), ("active", colors.blue_pale),
                    ("disabled", colors.surface)],
        foreground=[("disabled", colors.disabled), ("active", colors.blue)],
        bordercolor=[("disabled", colors.border), ("active", colors.blue)],
        lightcolor=[("active", colors.blue_pale)], darkcolor=[("active", colors.blue_pale)],
    )
    # Small borderless icon buttons that sit inside fields, such as clear and show.
    style.configure(
        "Icon.TButton", padding=(7, 5), background=colors.surface,
        bordercolor=colors.surface, relief="flat", focuscolor=colors.surface, **flat,
    )
    style.map(
        "Icon.TButton",
        background=[("pressed", colors.blue_selection), ("active", colors.blue_pale)],
        bordercolor=[("active", colors.blue_pale)],
        lightcolor=[("active", colors.blue_pale)], darkcolor=[("active", colors.blue_pale)],
    )
    style.configure(
        "Primary.TButton", padding=(20, 9), font=font(10, bold=True),
        background=colors.blue, foreground=colors.surface,
        bordercolor=colors.blue, relief="flat",
    )
    style.map(
        "Primary.TButton",
        background=[("pressed", colors.blue_pressed), ("active", colors.blue_hover),
                    ("disabled", colors.blue_selection)],
        foreground=[("disabled", colors.disabled)],
    )
    style.configure(
        "Card.Treeview", rowheight=32, font=font(), background=colors.surface,
        fieldbackground=colors.surface, foreground=colors.text, borderwidth=0,
        bordercolor=colors.surface, relief="flat", indent=16, **flat,
    )
    # Drop the sunken field border: the surrounding card already frames it.
    style.layout("Card.Treeview", [("Treeview.padding", {"sticky": "nswe", "children": [
        ("Treeview.treearea", {"sticky": "nswe"})]})])
    style.map(
        "Card.Treeview",
        background=[("selected", colors.blue_selection)],
        foreground=[("selected", colors.text)],
    )
    # The PDF list changes its selected colors when the selected book fails.
    # Give it a separate style so the bookmark/result trees keep their normal
    # blue selection, and selected failures remain visibly red.
    style.map(
        "PdfList.Card.Treeview",
        background=[("selected", colors.blue_selection)],
        foreground=[("selected", colors.text)],
    )
    style.configure(
        "Card.Treeview.Heading", padding=(8, 7), font=font(9, bold=True),
        background=colors.surface_tint, foreground=colors.muted,
        bordercolor=colors.border, relief="flat",
        lightcolor=colors.surface_tint, darkcolor=colors.surface_tint,
    )
    style.map("Card.Treeview.Heading",
              background=[("active", colors.blue_pale)],
              lightcolor=[("active", colors.blue_pale)],
              darkcolor=[("active", colors.blue_pale)])

    # Flat tabs: the selected tab is a pale-blue pill, the client area has no frame.
    style.configure("Card.TNotebook", background=colors.surface, borderwidth=0,
                    bordercolor=colors.surface, tabmargins=(0, 0, 0, 8), **flat)
    style.configure("Card.TNotebook.Tab", padding=(14, 6), font=font(10),
                    background=colors.surface, foreground=colors.muted,
                    bordercolor=colors.surface, **flat)
    style.map(
        "Card.TNotebook.Tab",
        padding=[("selected", (14, 6))], expand=[("selected", (0, 0, 0, 0))],
        background=[("selected", colors.blue_pale), ("active", colors.surface_tint)],
        foreground=[("selected", colors.blue), ("active", colors.text)],
        bordercolor=[("selected", colors.blue_pale), ("active", colors.surface_tint)],
        lightcolor=[("selected", colors.blue_pale), ("active", colors.surface_tint)],
        darkcolor=[("selected", colors.blue_pale), ("active", colors.surface_tint)],
    )
    style.layout("Card.TNotebook.Tab", [("Notebook.tab", {"sticky": "nswe", "children": [
        ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
            ("Notebook.label", {"side": "top", "sticky": ""})]})]})])

    _create_indicators(root, style, colors)
    for kind in ("Checkbutton", "Radiobutton"):
        name = f"App.T{kind}"
        style.configure(name, background=colors.surface, foreground=colors.text,
                        padding=(0, 3), focuscolor=colors.blue)
        style.map(name, background=[("active", colors.surface)],
                  foreground=[("disabled", colors.disabled)])
        style.layout(name, [(f"{kind}.padding", {"sticky": "nswe", "children": [
            (f"App.{kind}.indicator", {"side": "left", "sticky": ""}),
            (f"{kind}.focus", {"side": "left", "sticky": "w", "children": [
                (f"{kind}.label", {"sticky": "nswe"})]})]})])

    # Thin, arrowless scrollbar whose thumb disappears when nothing scrolls.
    style.layout("App.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
        "sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure(
        "App.Vertical.TScrollbar", background=colors.scroll_thumb,
        troughcolor=colors.surface, bordercolor=colors.surface,
        lightcolor=colors.scroll_thumb, darkcolor=colors.scroll_thumb,
        gripcount=0, arrowsize=8, width=8, relief="flat",
    )
    style.map(
        "App.Vertical.TScrollbar",
        background=[("disabled", colors.surface), ("active", colors.scroll_thumb_active)],
        lightcolor=[("disabled", colors.surface), ("active", colors.scroll_thumb_active)],
        darkcolor=[("disabled", colors.surface), ("active", colors.scroll_thumb_active)],
    )
    root.update_idletasks()
    return style


def _indicator_image(kind: str, *, checked: bool, disabled: bool,
                     colors: Palette, size: int = 16, gap: int = 8) -> Image.Image:
    """Draw an antialiased checkbox or radio indicator with trailing spacing."""
    scale = 4
    side = size * scale
    image = Image.new("RGBA", ((size + gap) * scale, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    stroke = 3 * scale // 2
    box = (scale, scale, side - scale - 1, side - scale - 1)
    active = colors.disabled if disabled else colors.blue
    outline = active if checked else (colors.border_strong if not disabled else colors.border)
    if kind == "Checkbutton":
        fill = active if checked else (colors.surface_tint if disabled else colors.surface)
        draw.rounded_rectangle(box, radius=4 * scale, fill=fill, outline=outline, width=stroke)
        if checked:
            draw.line([(0.28 * side, 0.52 * side), (0.44 * side, 0.67 * side),
                       (0.73 * side, 0.36 * side)], fill=colors.surface,
                      width=2 * scale, joint="curve")
    else:
        fill = colors.surface_tint if disabled else colors.surface
        draw.ellipse(box, fill=fill, outline=outline, width=stroke)
        if checked:
            radius, centre = side * 0.21, side / 2
            draw.ellipse((centre - radius, centre - radius, centre + radius, centre + radius),
                         fill=active)
    return image.resize((size + gap, size), Image.Resampling.LANCZOS)


def _create_indicators(root: tk.Misc, style: ttk.Style, colors: Palette) -> None:
    images = getattr(root, "_app_indicator_images", None)
    if images is None:
        images = {}
        for kind in ("Checkbutton", "Radiobutton"):
            for checked in (False, True):
                for disabled in (False, True):
                    images[kind, checked, disabled] = ImageTk.PhotoImage(
                        _indicator_image(kind, checked=checked, disabled=disabled,
                                         colors=colors), master=root)
        # Tk images are freed once Python drops them; keep them on the root.
        root._app_indicator_images = images  # type: ignore[attr-defined]
    for kind in ("Checkbutton", "Radiobutton"):
        element = f"App.{kind}.indicator"
        if element in style.element_names():
            continue
        style.element_create(
            element, "image", images[kind, False, False],
            ("disabled", "selected", images[kind, True, True]),
            ("disabled", images[kind, False, True]),
            ("selected", images[kind, True, False]),
        )


_BREAK_TOKEN = re.compile(r"[A-Za-z0-9_.%:/\\\-]+|\s+|.", re.S)
_NO_LINE_START = frozenset("，。；：、！？）》」』】,.;:!?)%")


def wrap_text(text: str, measure: Callable[[str], int], width: int) -> str:
    """Wrap mixed Chinese and Latin text to ``width`` pixels.

    Tk only breaks lines at spaces, so "现有 4 条书签…" would break after
    "4" and leave a ragged first line.  This breaks between Chinese
    characters, keeps Latin words and numbers together, and never starts a
    line with closing punctuation.
    """
    if width <= 0:
        return text
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        tokens: list[str] = []
        for token in _BREAK_TOKEN.findall(paragraph):
            if len(token) > 1 and not token.isspace() and measure(token) > width:
                tokens.extend(token)  # An over-long word or path breaks anywhere.
            else:
                tokens.append(token)
        for token in tokens:
            if (not line or token.isspace() or token in _NO_LINE_START
                    or measure(line + token) <= width):
                line += token
                continue
            lines.append(line.rstrip())
            line = token
        lines.append(line.rstrip())
    return "\n".join(lines)


class WrapLabel(tk.Label):
    """A ``tk.Label`` showing a ``StringVar`` wrapped with :func:`wrap_text`.

    ``configure(wraplength=...)`` sets the wrapping width in pixels, like a
    normal label, so resize handlers can stay unchanged.
    """

    def __init__(self, master: tk.Misc, *, textvariable: tk.StringVar,
                 wraplength: int = 220, **kwargs: object) -> None:
        super().__init__(master, **kwargs)
        self._variable = textvariable
        self._wrap_width = int(wraplength)
        self._measure_font = tkfont.Font(root=self, font=self.cget("font"))
        self._trace = textvariable.trace_add("write", lambda *_: self._refresh())
        self.bind("<Destroy>", self._forget_trace, add="+")
        self._refresh()

    def _forget_trace(self, event: tk.Event) -> None:
        if event.widget is self and self._trace:
            try:
                self._variable.trace_remove("write", self._trace)
            except tk.TclError:
                pass
            self._trace = ""

    def _refresh(self) -> None:
        text = wrap_text(self._variable.get(), self._measure_font.measure, self._wrap_width)
        super().configure(text=text)

    def configure(self, cnf: object = None, **kwargs: object) -> object:
        width = kwargs.pop("wraplength", None)
        new_font = kwargs.get("font")
        # With no arguments, return the option table like any Tk widget.
        result = (super().configure(cnf, **kwargs) if cnf or kwargs or width is None
                  else None)
        if new_font is not None:
            self._measure_font = tkfont.Font(root=self, font=self.cget("font"))
        if width is not None or new_font is not None:
            if width is not None:
                self._wrap_width = int(width)
            self._refresh()
        return result

    config = configure


def rounded_background(
    width: int,
    height: int,
    *,
    radius: int = 9,
    fill: str = COLORS.surface,
    outline: str | None = COLORS.border,
    stroke: int = 1,
    scale: int = 3,
) -> Image.Image:
    """Return an antialiased RGBA rounded rectangle for a Canvas background."""
    if width < 1 or height < 1:
        raise ValueError("width and height must be positive")
    if scale < 1:
        raise ValueError("scale must be positive")
    image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = max(stroke * scale // 2, 0) if outline else 0
    draw.rounded_rectangle(
        (inset, inset, width * scale - 1 - inset, height * scale - 1 - inset),
        radius=min(radius * scale, width * scale // 2, height * scale // 2),
        fill=fill, outline=outline, width=max(stroke * scale, 1) if outline else 1,
    )
    if scale == 1:
        return image
    return image.resize((width, height), Image.Resampling.LANCZOS)


def render_icon(name: str, *, size: int = 20, color: str = COLORS.blue) -> Image.Image:
    """Tint and resize a bundled Tabler outline icon."""
    if size < 1:
        raise ValueError("size must be positive")
    aliases = {"file": "pdf", "chevron-left": "left", "chevron-right": "right",
               "x": "close"}
    name = aliases.get(name, name)
    known = {
        "pdf", "folder", "eye", "bookmark", "settings", "search", "play",
        "stop", "info", "list", "report", "left", "right", "plus",
        "minus", "expand", "close", "check", "key", "folder-open",
        "file-report", "file-text", "zoom-in", "zoom-out",
    }
    if name not in known:
        raise ValueError(f"unknown icon: {name}")
    mask = _icon_alpha(name).resize((size, size), Image.Resampling.LANCZOS)
    image = Image.new("RGBA", (size, size), color)
    image.putalpha(mask)
    return image


@lru_cache(maxsize=None)
def _icon_alpha(name: str) -> Image.Image:
    path = Path(__file__).resolve().parent / "assets" / "icons" / f"{name}.png"
    with Image.open(path) as source:
        return source.getchannel("A").copy()


def icon(name: str, *, size: int = 20, color: str = COLORS.blue,
         master: tk.Misc | None = None) -> ImageTk.PhotoImage:
    """Return a Tk image; retain the image on the widget that displays it."""
    return ImageTk.PhotoImage(render_icon(name, size=size, color=color), master=master)


class RoundedButton(tk.Canvas):
    """A keyboard-accessible rounded button for high-visibility controls.

    ``kind`` is ``primary``, ``secondary``, or ``quiet``.  The class supports
    ``configure(state='disabled')`` like ttk.Button; keep using ttk for compact
    form controls if a custom button is not needed.
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        text: str,
        command: Callable[[], object] | None = None,
        kind: Literal["primary", "secondary", "quiet"] = "secondary",
        icon_name: str | None = None,
        width: int | None = None,
        height: int = 38,
        radius: int = 8,
        state: Literal["normal", "disabled"] = "normal",
        colors: Palette = COLORS,
    ) -> None:
        if kind not in {"primary", "secondary", "quiet"}:
            raise ValueError(f"unknown button kind: {kind}")
        self._text = text
        self._command = command
        self._kind = kind
        self._icon_name = icon_name
        self._radius = radius
        self._state = state
        self._colors = colors
        self._hovered = False
        self._pressed = False
        self._font = tkfont.Font(root=master, family=FONT_FAMILY, size=10,
                                 weight="bold" if kind == "primary" else "normal")
        intrinsic = self._font.measure(text) + 29 + (24 if icon_name else 0)
        self._button_width = width or intrinsic
        self._button_height = height
        self._background_image: ImageTk.PhotoImage | None = None
        self._icon_image: ImageTk.PhotoImage | None = None
        super().__init__(master, width=self._button_width, height=height,
                         highlightthickness=0, bd=0,
                         bg=master.cget("bg") if "bg" in master.keys() else colors.background,
                         cursor="hand2" if state == "normal" else "arrow",
                         takefocus=True)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", self._invoke_key)
        self.bind("<space>", self._invoke_key)
        self.bind("<FocusIn>", lambda _event: self._draw())
        self.bind("<FocusOut>", lambda _event: self._draw())
        self._draw()

    def _appearance(self) -> tuple[str, str, str | None]:
        if self._state == "disabled":
            return self._colors.surface_tint, self._colors.disabled, self._colors.border
        if self._kind == "primary":
            fill = (self._colors.blue_pressed if self._pressed else
                    self._colors.blue_hover if self._hovered else self._colors.blue)
            return fill, self._colors.surface, None
        if self._kind == "quiet":
            return (self._colors.blue_pale if self._hovered else self._colors.surface,
                    self._colors.blue, None)
        return (self._colors.blue_pale if self._hovered else self._colors.surface,
                self._colors.blue if self._hovered else self._colors.text,
                self._colors.blue if self._hovered else self._colors.border)

    def _draw(self) -> None:
        fill, foreground, border = self._appearance()
        self.delete("all")
        background = rounded_background(self._button_width, self._button_height,
                                        radius=self._radius, fill=fill, outline=border)
        self._background_image = ImageTk.PhotoImage(background, master=self)
        self.create_image(0, 0, image=self._background_image, anchor="nw")
        text_width = self._font.measure(self._text)
        group_width = text_width + (22 if self._icon_name else 0)
        left = (self._button_width - group_width) / 2
        if self._icon_name:
            self._icon_image = icon(self._icon_name, size=16, color=foreground, master=self)
            icon_x = self._button_width / 2 if not self._text else left + 8
            self.create_image(icon_x, self._button_height / 2,
                              image=self._icon_image, anchor="center")
            left += 22
        if self._text:
            self.create_text(left, self._button_height / 2, text=self._text,
                             fill=foreground, font=self._font, anchor="w")
        if self.focus_get() is self:
            self.create_rectangle(3, 3, self._button_width - 4, self._button_height - 4,
                                  outline=self._colors.blue if self._kind != "primary"
                                  else self._colors.surface, dash=(2, 2))

    def _enter(self, _event: tk.Event) -> None:
        self._hovered = True
        self._draw()

    def _leave(self, _event: tk.Event) -> None:
        self._hovered = False
        self._pressed = False
        self._draw()

    def _press(self, _event: tk.Event) -> None:
        if self._state == "normal":
            self._pressed = True
            self.focus_set()
            self._draw()

    def _release(self, event: tk.Event) -> None:
        invoke = self._pressed and 0 <= event.x < self._button_width and 0 <= event.y < self._button_height
        self._pressed = False
        self._draw()
        if invoke:
            self.invoke()

    def _invoke_key(self, _event: tk.Event) -> None:
        self.invoke()

    def invoke(self) -> None:
        if self._state == "normal" and self._command is not None:
            self._command()

    def __getitem__(self, key: str) -> object:
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return super().__getitem__(key)

    def instate(self, states: tuple[str, ...] | list[str]) -> bool:
        for state in states:
            if state == "disabled" and self._state != "disabled":
                return False
            if state == "!disabled" and self._state == "disabled":
                return False
        return True

    def configure(self, cnf: object = None, **kwargs: object) -> object:
        state = kwargs.pop("state", None)
        if state is not None:
            if state not in {"normal", "disabled"}:
                raise ValueError("state must be 'normal' or 'disabled'")
            self._state = state
            self["cursor"] = "hand2" if state == "normal" else "arrow"
            self._draw()
        text = kwargs.pop("text", None)
        if text is not None:
            self._text = str(text)
            self._draw()
        command = kwargs.pop("command", None)
        if command is not None:
            self._command = command  # type: ignore[assignment]
        icon_name = kwargs.pop("icon_name", None)
        if icon_name is not None:
            self._icon_name = str(icon_name)
            self._draw()
        if cnf is not None or kwargs:
            return super().configure(cnf, **kwargs)
        return None

    config = configure
