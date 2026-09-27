"""Small visual building blocks for the desktop workbench.

The palette follows the supplied light-blue prototype.  Icons use local
Tabler outline assets and are tinted to match the widget state.  Keep returned
``PhotoImage`` objects referenced by the owning widget.
"""

from __future__ import annotations

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
    style.configure(".", font=font(), foreground=colors.text, background=colors.surface)
    style.configure(
        "App.TEntry", padding=(10, 7), fieldbackground=colors.surface,
        background=colors.surface, foreground=colors.text,
        bordercolor=colors.border, lightcolor=colors.border,
        darkcolor=colors.border, relief="flat",
    )
    style.map("App.TEntry", bordercolor=[("focus", colors.blue)])
    style.configure(
        "App.TCombobox", padding=(8, 6), fieldbackground=colors.surface,
        background=colors.surface, foreground=colors.text,
        bordercolor=colors.border, arrowcolor=colors.muted,
    )
    style.map("App.TCombobox", bordercolor=[("focus", colors.blue)])
    style.configure(
        "App.TButton", padding=(14, 7), font=font(),
        background=colors.surface, foreground=colors.text,
        bordercolor=colors.border, relief="flat",
    )
    style.map(
        "App.TButton",
        background=[("active", colors.blue_pale), ("disabled", colors.surface_tint)],
        foreground=[("disabled", colors.disabled)],
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
        "Card.Treeview", rowheight=31, font=font(), background=colors.surface,
        fieldbackground=colors.surface, foreground=colors.text, borderwidth=0,
    )
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
        "Card.Treeview.Heading", padding=(8, 8), font=font(9, bold=True),
        background=colors.surface_tint, foreground=colors.muted,
        bordercolor=colors.border, relief="flat",
    )
    style.configure("App.TCheckbutton", background=colors.surface, foreground=colors.text)
    style.map("App.TCheckbutton", background=[("active", colors.surface)])
    style.configure("App.TRadiobutton", background=colors.surface, foreground=colors.text)
    style.map("App.TRadiobutton", background=[("active", colors.surface)])
    style.configure(
        "App.Vertical.TScrollbar", background=colors.surface_tint,
        troughcolor=colors.surface, arrowcolor=colors.muted,
        bordercolor=colors.border, lightcolor=colors.surface_tint,
        darkcolor=colors.surface_tint, gripcount=0, width=10,
    )
    root.update_idletasks()
    return style


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
