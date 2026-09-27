"""Locate bundled helpers in a frozen app, or use development installations."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _bundle_root() -> Path | None:
    root = getattr(sys, "_MEIPASS", None)
    return Path(root) if root else None


def find_executable(name: str) -> str | None:
    """Prefer a packaged Poppler executable, then search the normal PATH."""
    root = _bundle_root()
    if root is not None:
        names = [name] if name.lower().endswith(".exe") else [name + ".exe", name]
        directories = (
            root / "tools",
            root / "tools" / "bin",
            root / "tools" / "poppler" / "bin",
            root / "tools" / "poppler" / "Library" / "bin",
            root / "poppler" / "bin",
            root / "poppler" / "Library" / "bin",
            root,
        )
        for directory in directories:
            for filename in names:
                candidate = directory / filename
                if candidate.is_file():
                    return str(candidate)
    return shutil.which(name)


def ocr_script_path() -> Path:
    """Resolve the PowerShell script in a PyInstaller bundle or source tree."""
    root = _bundle_root()
    candidates = [] if root is None else [
        root / "bookmarker" / "ocr_windows.ps1",
        root / "ocr_windows.ps1",
    ]
    candidates.append(Path(__file__).with_name("ocr_windows.ps1"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Windows OCR script ocr_windows.ps1 was not found in the application bundle")
