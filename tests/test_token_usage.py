from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from bookmarker.__main__ import _write_summary
from bookmarker.pipeline import BookResult


class TokenUsageReportTest(unittest.TestCase):
    def test_result_and_csv_preserve_actual_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.pdf"
            result = BookResult(str(source), "success")
            result.api_usage.update({
                "prompt_tokens": 321, "completion_tokens": 42, "total_tokens": 363,
                "reported_responses": 2, "unreported_responses": 0,
            })
            report = root / "report.jsonl"
            report.write_text(json.dumps(result.as_dict()) + "\n", encoding="utf-8")
            summary = root / "summary.csv"
            _write_summary(report, summary, [source])
            with summary.open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["prompt_tokens"], "321")
            self.assertEqual(row["completion_tokens"], "42")
            self.assertEqual(row["total_tokens"], "363")
            self.assertEqual(row["reported_responses"], "2")

    def test_old_report_without_usage_leaves_csv_cells_blank(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.pdf"
            report = root / "report.jsonl"
            report.write_text(json.dumps({"source": str(source), "status": "success"}) + "\n",
                              encoding="utf-8")
            summary = root / "summary.csv"
            _write_summary(report, summary, [source])
            with summary.open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["total_tokens"], "")
            self.assertEqual(row["reported_responses"], "")


if __name__ == "__main__":
    unittest.main()
