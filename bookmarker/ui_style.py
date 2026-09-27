"""Small, dependency-free visual building blocks for the desktop workbench.

The palette follows the supplied light-blue prototype.  Icons are drawn from
simple vector strokes at four times their display size, so they stay crisp on
Windows display scaling without shipping font or image assets.  Keep returned
``PhotoImage`` objects referenced by the owning widget.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
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
    disabled: str = "#A7B6CD"


COLORS = Palette()
FONT_FAMILY = "Segoe UI"


def font(size: int = 10, *, bold: bool = False) -> tuple[str, int, str]:
    """Return a Tk font tuple matching the workbench typography."""
    return FONT_FAMILY, size, "bold" if bold else "normal"


def apply_theme(root: tk.Misc, colors: Palette = COLORS) -> ttk.Style:
    """Configure the standard ttk styles used by the PDF workbench.

    This uses the built-in ``clam`` theme so packaging needs no extra files.
    Canvas controls can use :class:`RoundedButton` for closer prototype edges.
    """
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
    style.configure(
        "Card.Treeview.Heading", padding=(8, 8), font=font(9, bold=True),
        background=colors.surface_tint, foreground=colors.muted,
        bordercolor=colors.border, relief="flat",
    )
    style.configure("App.TCheckbutton", background=colors.surface, foreground=colors.text)
    style.map("App.TCheckbutton", background=[("active", colors.surface)])
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
    """Draw a 20-pixel-style line icon as an RGBA Pillow image.

    Supported names: ``pdf``, ``folder``, ``eye``, ``bookmark``, ``settings``,
    ``search``, ``play``, ``stop``, ``info``, ``list``, ``report``, ``left``,
    ``right``, ``plus``, ``minus``, ``expand``, ``close``, ``check``.
    """
    if size < 1:
        raise ValueError("size must be positive")
    aliases = {"file": "pdf", "chevron-left": "left", "chevron-right": "right",
               "x": "close", "zoom-in": "plus", "zoom-out": "minus"}
    name = aliases.get(name, name)
    known = {
        "pdf", "folder", "eye", "bookmark", "settings", "search", "play",
        "stop", "info", "list", "report", "left", "right", "plus",
        "minus", "expand", "close", "check",
    }
    if name not in known:
        raise ValueError(f"unknown icon: {name}")

    factor = 4
    image = Image.new("RGBA", (size * factor, size * factor), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = size * factor / 24
    point = lambda x, y: (round(x * unit), round(y * unit))
    stroke = max(round(1.9 * unit), 1)

    def line(*coords: tuple[float, float], width: int = stroke) -> None:
        draw.line([point(x, y) for x, y in coords], fill=color, width=width, joint="curve")

    def rounded(box: tuple[float, float, float, float], radius: float = 2.0,
                *, fill: str | None = None, width: int = stroke) -> None:
        draw.rounded_rectangle((*point(box[0], box[1]), *point(box[2], box[3])),
                               radius=round(radius * unit), outline=color, fill=fill,
                               width=width)

    if name == "pdf":
        line((6, 2.5), (15, 2.5), (19, 6.5), (19, 21), (6, 21), (6, 2.5))
        line((15, 2.5), (15, 6.5), (19, 6.5))
        rounded((8, 11, 17, 17), 1.2, fill=color, width=0)
        line((9.5, 14), (15.5, 14), width=max(1, round(unit)))
    elif name == "folder":
        line((2.5, 6), (9.5, 6), (11.5, 8), (21, 8), (21, 19), (2.5, 19), (2.5, 6))
        line((2.5, 9), (21, 9))
    elif name == "eye":
        draw.ellipse((*point(2.5, 6), *point(21.5, 18)), outline=color, width=stroke)
        draw.ellipse((*point(9, 9), *point(15, 15)), fill=color)
    elif name == "bookmark":
        line((6, 3), (18, 3), (18, 21), (12, 17), (6, 21), (6, 3))
    elif name == "settings":
        draw.ellipse((*point(5, 5), *point(19, 19)), outline=color, width=stroke)
        draw.ellipse((*point(9, 9), *point(15, 15)), outline=color, width=stroke)
        for x1, y1, x2, y2 in ((12, 1, 12, 5), (12, 19, 12, 23),
                                (1, 12, 5, 12), (19, 12, 23, 12),
                                (4, 4, 7, 7), (17, 17, 20, 20),
                                (17, 7, 20, 4), (4, 20, 7, 17)):
            line((x1, y1), (x2, y2))
    elif name == "search":
        draw.ellipse((*point(3, 3), *point(15, 15)), outline=color, width=stroke)
        line((14, 14), (21, 21))
    elif name == "play":
        draw.polygon([point(7, 4), point(20, 12), point(7, 20)], fill=color)
    elif name == "stop":
        rounded((5, 5, 19, 19), 2, fill=color, width=0)
    elif name == "info":
        draw.ellipse((*point(3, 3), *point(21, 21)), outline=color, width=stroke)
        draw.ellipse((*point(11, 7), *point(13, 9)), fill=color)
        line((12, 11), (12, 17))
    elif name in {"list", "report"}:
        if name == "report":
            rounded((4, 2.5, 20, 21), 2)
        for y in (7, 12, 17):
            draw.ellipse((*point(6 if name == "list" else 7, y - 1),
                          *point(8 if name == "list" else 9, y + 1)), fill=color)
            line((10, y), (21 if name == "list" else 17, y))
    elif name == "left":
        line((15, 5), (8, 12), (15, 19))
    elif name == "right":
        line((9, 5), (16, 12), (9, 19))
    elif name in {"plus", "minus"}:
        line((5, 12), (19, 12))
        if name == "plus":
            line((12, 5), (12, 19))
    elif name == "expand":
        line((4, 10), (4, 4), (10, 4))
        line((14, 4), (20, 4), (20, 10))
        line((20, 14), (20, 20), (14, 20))
        line((10, 20), (4, 20), (4, 14))
    elif name == "close":
        line((5, 5), (19, 19))
        line((19, 5), (5, 19))
    elif name == "check":
        line((4, 12), (10, 18), (20, 6))
    return image.resize((size, size), Image.Resampling.LANCZOS)


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
        if cnf is not None or kwargs:
            return super().configure(cnf, **kwargs)
        return None

    config = configure
