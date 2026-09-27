from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookmarker.resources import find_executable, ocr_script_path


class ResourceLookupTest(unittest.TestCase):
    def test_bundled_poppler_wins_over_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundled = Path(directory) / "tools" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            with patch.object(sys, "_MEIPASS", directory, create=True), \
                    patch("bookmarker.resources.shutil.which", return_value="C:/other/pdftoppm.exe") as which:
                self.assertEqual(find_executable("pdftoppm"), str(bundled))
                which.assert_not_called()

    def test_uses_path_when_bundle_has_no_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, "_MEIPASS", directory, create=True), \
                    patch("bookmarker.resources.shutil.which", return_value="C:/poppler/pdftotext.exe") as which:
                self.assertEqual(find_executable("pdftotext"), "C:/poppler/pdftotext.exe")
                which.assert_called_once_with("pdftotext")

    def test_ocr_script_in_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "bookmarker" / "ocr_windows.ps1"
            script.parent.mkdir()
            script.touch()
            with patch.object(sys, "_MEIPASS", directory, create=True):
                self.assertEqual(ocr_script_path(), script)


if __name__ == "__main__":
    unittest.main()
