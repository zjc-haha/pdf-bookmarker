"""Shared entry point for the installed GUI and command-line executables."""

from __future__ import annotations

import sys
from pathlib import Path

# A freezer and `python bookmarker/app.py` execute this file as a script rather
# than a package module. Make the containing project importable in both cases.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookmarker.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
