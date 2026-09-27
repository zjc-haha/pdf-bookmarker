"""Console helpers launched by the windowed app must not create a console."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bookmarker.extract import Extractor


class HiddenSubprocessTest(unittest.TestCase):
    def assert_hidden(self, call) -> None:
        self.assertEqual(call.kwargs["creationflags"],
                         getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_ocr_helpers_hide_poppler_and_powershell_consoles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "page.png"
            extractor = Extractor(Path("book.pdf"), Path(temporary))

            def render(_arguments, **_kwargs):
                destination.touch()
                return SimpleNamespace(returncode=0)

            with patch("bookmarker.extract.find_executable", return_value="pdftoppm.exe"), \
                 patch("bookmarker.extract.subprocess.run", side_effect=render) as run:
                extractor._render(1, destination, scale=100)
            self.assert_hidden(run.call_args)

            with patch("bookmarker.extract.find_executable", return_value="powershell.exe"), \
                 patch("bookmarker.extract.ocr_script_path", return_value=Path("ocr.ps1")), \
                 patch("bookmarker.extract.subprocess.run", return_value=SimpleNamespace(
                     returncode=0, stdout='{"lines": []}', stderr="")) as run:
                self.assertEqual(extractor._run_ocr_language(destination, "zh-Hans-CN"),
                                 {"lines": []})
            self.assert_hidden(run.call_args)

if __name__ == "__main__":
    unittest.main()
