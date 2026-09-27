"""Per-run JSONL report reading without a PDF or GUI dependency."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from bookmarker.gui_report import RunReport


def _line(source: str, status: str) -> bytes:
    return (json.dumps({"source": source, "status": status}, ensure_ascii=False)
            + "\n").encode("utf-8")


class RunReportTest(unittest.TestCase):
    def test_reads_only_complete_rows_added_after_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bookmarker-report.jsonl"
            old = _line("previous.pdf", "failed")
            path.write_bytes(old)
            report = RunReport(path)
            self.assertEqual(report.start_offset, len(old))
            self.assertEqual(report.read_new(), ())

            with path.open("ab") as stream:
                stream.write(_line("one.pdf", "success"))
                stream.write(_line("two.pdf", "needs_review"))
            self.assertEqual([row["source"] for row in report.read_new()],
                             ["one.pdf", "two.pdf"])
            self.assertEqual(report.read_new(), ())
            self.assertEqual(set(report.records), {"one.pdf", "two.pdf"})
            self.assertEqual(report.status_counts,
                             {"success": 1, "needs_review": 1})

    def test_holds_partial_utf8_line_and_counts_latest_status_per_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bookmarker-report.jsonl"
            report = RunReport(path)
            encoded = _line("书.pdf", "needs_review")
            split = encoded.index("书".encode("utf-8")) + 1
            path.write_bytes(encoded[:split])
            self.assertEqual(report.read_new(), ())
            self.assertEqual(report.records, {})

            with path.open("ab") as stream:
                stream.write(encoded[split:])
                stream.write(_line("书.pdf", "success"))
            self.assertEqual([row["status"] for row in report.read_new()],
                             ["needs_review", "success"])
            self.assertEqual(report.records["书.pdf"]["status"], "success")
            self.assertEqual(report.status_counts, {"success": 1})

    def test_ignores_preexisting_unterminated_line_and_malformed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bookmarker-report.jsonl"
            path.write_bytes(b'{"source":"old.pdf"')
            report = RunReport(path)
            with path.open("ab") as stream:
                stream.write(b',"status":"failed"}\n')
                stream.write(b'not json\n')
                stream.write(b'[1, 2]\n')
                stream.write(b'{"source":"missing-status.pdf"}\n')
                stream.write(_line("new.pdf", "skipped"))
            self.assertEqual(report.read_new(),
                             ({"source": "new.pdf", "status": "skipped"},))
            self.assertEqual(report.status_counts, {"skipped": 1})

    def test_detects_truncation_and_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bookmarker-report.jsonl"
            report = RunReport(path)
            path.write_bytes(_line("first.pdf", "success"))
            report.read_new()
            self.assertIn("first.pdf", report.records)

            path.write_bytes(_line("new.pdf", "failed"))
            self.assertEqual(report.read_new(),
                             ({"source": "new.pdf", "status": "failed"},))
            self.assertEqual(set(report.records), {"new.pdf"})

            replacement = path.with_suffix(".new")
            replacement.write_bytes(_line("replaced.pdf", "needs_review"))
            os.replace(replacement, path)
            self.assertEqual(report.read_new(),
                             ({"source": "replaced.pdf", "status": "needs_review"},))
            self.assertEqual(report.status_counts, {"needs_review": 1})

    def test_detects_rewrite_that_regrows_past_previous_offset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bookmarker-report.jsonl"
            path.write_bytes(_line("historic.pdf", "success"))
            report = RunReport(path)
            path.write_bytes(_line("a-much-longer-new-file-name.pdf", "failed"))
            self.assertGreater(path.stat().st_size, report.start_offset)
            self.assertEqual(report.read_new(),
                             ({"source": "a-much-longer-new-file-name.pdf",
                               "status": "failed"},))

    def test_missing_file_can_appear_and_disappear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bookmarker-report.jsonl"
            report = RunReport(path)
            self.assertEqual(report.start_offset, 0)
            self.assertEqual(report.read_new(), ())
            path.write_bytes(_line("one.pdf", "success"))
            self.assertEqual(len(report.read_new()), 1)
            path.unlink()
            self.assertEqual(report.read_new(), ())
            self.assertEqual(report.records, {})
            path.write_bytes(_line("two.pdf", "skipped"))
            self.assertEqual(report.read_new(),
                             ({"source": "two.pdf", "status": "skipped"},))


if __name__ == "__main__":
    unittest.main()
