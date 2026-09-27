from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from pypdf import PdfReader, PdfWriter

from bookmarker import __main__ as cli
from bookmarker import gui
from bookmarker import pipeline
from bookmarker.pipeline import BookResult
from bookmarker.toc import TocEntry


@contextmanager
def mocked_deepseek(process: Mock):
    module = types.ModuleType("bookmarker.deepseek")
    module.DEEPSEEK_MODEL = "deepseek-test"
    module.PROMPT_VERSION = "test-v1"
    module.process_book_deepseek = process
    with patch.dict(sys.modules, {"bookmarker.deepseek": module}), \
         patch.dict(cli.os.environ, {"DEEPSEEK_API_KEY": "test-secret-key"}):
        yield


class AppEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_root = Path(temporary.name) / "data"
        # CLI, GUI helpers and PDF writes must never use the real installation
        # data folder during tests.
        for target in ("bookmarker.storage.data_root", "bookmarker.__main__.data_root"):
            location = patch(target, return_value=self.data_root)
            location.start()
            self.addCleanup(location.stop)

    def _job_dir(self, source: Path, output: Path, *, overwrite: bool = False) -> Path:
        return cli.job_data_dir(source, output, overwrite)

    def test_done_files_ignores_malformed_rows_and_keeps_latest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.jsonl"
            report.write_bytes(
                b'not json\n[]\n{"source":"book.pdf","status":"failed"}\n'
                b'\xff\n{"source":"book.pdf","status":"success"}\n'
            )
            self.assertEqual(cli._done_files(report)["book.pdf"]["status"], "success")

    def test_batch_keeps_partial_summary_when_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books"
            output = root / "bookmarked"
            source.mkdir()
            (source / "a.pdf").touch()
            (source / "b.pdf").touch()
            process = Mock(side_effect=[BookResult(str(source / "a.pdf"), "success"),
                                        KeyboardInterrupt()])
            with mocked_deepseek(process):
                with redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                    cli.main(["batch", str(source), "--output", str(output)])
            summary = (self._job_dir(source, output) / "bookmarker-summary.csv").read_text(
                encoding="utf-8-sig")
            self.assertIn("a.pdf", summary)
            self.assertNotIn("b.pdf", summary)
            self.assertFalse(output.exists())

    def test_stop_request_finishes_current_pdf_before_ending_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books"
            source.mkdir()
            for name in ("a.pdf", "b.pdf"):
                (source / name).touch()
            stop_file = self._job_dir(source, root / "output") / "stop-request"

            def process_one(path: Path, _output: Path, _cache: Path, **_kwargs: object) -> BookResult:
                stop_file.parent.mkdir(parents=True, exist_ok=True)
                stop_file.write_text("stop", encoding="utf-8")
                return BookResult(str(path), "success")

            process = Mock(side_effect=process_one)
            with mocked_deepseek(process), redirect_stdout(io.StringIO()):
                code = cli.main(["batch", str(source), "--output", str(root / "output"),
                                 "--stop-file", str(stop_file)])
            self.assertEqual(code, 130)
            self.assertEqual(process.call_count, 1)
            report = self._job_dir(source, root / "output") / "bookmarker-report.jsonl"
            self.assertEqual(len(report.read_text(encoding="utf-8").splitlines()), 1)

    def test_accept_review_processes_only_the_confirmed_pdf_in_the_same_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books"
            output = root / "output"
            source.mkdir()
            for name in ("a.pdf", "b.pdf", "c.pdf"):
                (source / name).touch()
            seen: list[tuple[Path, Path, bool]] = []

            def confirm(path: Path, _output: Path, cache: Path, **kwargs: object) -> BookResult:
                seen.append((path, cache, bool(kwargs["accept_review"])))
                result = BookResult(str(path), "success")
                result.review_accepted = True
                result.warnings = ["目录页不连续，需要人工确认是否漏页"]
                return result

            process = Mock(side_effect=confirm)
            stdout = io.StringIO()
            with mocked_deepseek(process), redirect_stdout(stdout):
                code = cli.main(["batch", str(source), "--output", str(output),
                                 "--accept-review", str(source / "b.pdf")])
            self.assertEqual(code, 0)
            self.assertEqual([(path.name, accepted) for path, _, accepted in seen],
                             [("b.pdf", True)])
            job = self._job_dir(source, output)
            self.assertEqual(seen[0][1].parents[2], job)
            log = stdout.getvalue()
            self.assertIn("第 2/3 本：b.pdf；正在处理", log)
            self.assertIn("已按人工确认写入", log)
            rows = [json.loads(line) for line in
                    (job / "bookmarker-report.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(Path(row["source"]).name, row["review_accepted"]) for row in rows],
                             [("b.pdf", True)])

    def test_accept_review_rejects_dry_run_and_pdfs_outside_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books"
            source.mkdir()
            (source / "a.pdf").touch()
            process = Mock()
            for extra in (["--accept-review", str(source / "a.pdf"), "--dry-run"],
                          ["--accept-review", str(root / "other.pdf")]):
                with self.subTest(extra=extra), mocked_deepseek(process), \
                        redirect_stderr(io.StringIO()) as errors:
                    code = cli.main(["batch", str(source), "--output", str(root / "output"),
                                     *extra])
                self.assertEqual(code, 2)
                self.assertTrue(errors.getvalue().strip())
            process.assert_not_called()

    def test_pending_overwrite_must_be_resolved_before_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "book.pdf"
            source.touch()
            error_output = io.StringIO()
            with patch("bookmarker.pipeline.recover_pending_overwrites",
                       side_effect=RuntimeError("原 PDF 需要人工核对")), \
                 patch("bookmarker.deepseek.process_book_deepseek") as process, \
                 redirect_stderr(error_output):
                code = cli.main(["single", str(source), "--overwrite-original"])
            self.assertEqual(code, 2)
            self.assertIn("原 PDF 需要人工核对", error_output.getvalue())
            process.assert_not_called()

    def test_no_arguments_open_gui(self) -> None:
        with patch("bookmarker.gui.main") as run_gui:
            self.assertEqual(cli.main([]), 0)
        run_gui.assert_called_once_with()

    def test_cli_uses_utf8_for_redirected_output_only(self) -> None:
        stdout = Mock()
        stderr = Mock()
        stdout.isatty.return_value = False
        stderr.isatty.return_value = False
        with patch.object(cli.sys, "stdout", stdout), patch.object(cli.sys, "stderr", stderr), \
             patch("bookmarker.__main__.run_single", return_value=0):
            self.assertEqual(cli.main(["single", "中文.pdf", "--output", "输出"]), 0)
        stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

        stdout.reset_mock()
        stderr.reset_mock()
        stdout.isatty.return_value = True
        stderr.isatty.return_value = True
        with patch.object(cli.sys, "stdout", stdout), patch.object(cli.sys, "stderr", stderr), \
             patch("bookmarker.__main__.run_single", return_value=0):
            self.assertEqual(cli.main(["single", "中文.pdf", "--output", "输出"]), 0)
        stdout.reconfigure.assert_not_called()
        stderr.reconfigure.assert_not_called()

    def test_batch_arguments_reach_cli(self) -> None:
        with patch("bookmarker.__main__.run_batch", return_value=7) as run_batch:
            code = cli.main(["batch", "input", "--output", "output", "--dry-run",
                             "--verify-existing"])
        self.assertEqual(code, 7)
        args = run_batch.call_args.args[0]
        self.assertEqual(args.input, Path("input"))
        self.assertEqual(args.output, Path("output"))
        self.assertTrue(args.dry_run)
        self.assertTrue(args.verify_existing)
        self.assertFalse(args.skip_bookmarked)
        self.assertFalse(hasattr(args, "engine"))

    def test_single_arguments_reach_cli(self) -> None:
        with patch("bookmarker.__main__.run_single", return_value=7) as run_single:
            code = cli.main(["single", "one.PDF", "--output", "output"])
        self.assertEqual(code, 7)
        args = run_single.call_args.args[0]
        self.assertEqual(args.input, Path("one.PDF"))
        self.assertEqual(args.output, Path("output"))
        self.assertFalse(hasattr(args, "ocr"))
        self.assertFalse(args.verify_existing)
        self.assertFalse(args.overwrite_original)
        self.assertFalse(args.skip_bookmarked)

    def test_cli_rejects_ocr_options(self) -> None:
        for option in (("--engine", "ocr"), ("--ocr", "windows")):
            with self.subTest(option=option), redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as caught:
                cli.main(["single", "one.pdf", "--output", "reports", *option])
            self.assertEqual(caught.exception.code, 2)

    def test_skip_bookmarked_option_reaches_single_and_batch(self) -> None:
        for command, target in (("single", "one.pdf"), ("batch", "books")):
            with self.subTest(command=command), patch(
                    f"bookmarker.__main__.run_{command}", return_value=0) as runner:
                self.assertEqual(cli.main([command, target, "--output", "reports",
                                           "--skip-bookmarked"]), 0)
                self.assertTrue(runner.call_args.args[0].skip_bookmarked)

    def test_overwrite_option_reaches_single_and_batch(self) -> None:
        for command, target in (("single", "one.pdf"), ("batch", "books")):
            with self.subTest(command=command), patch(
                    f"bookmarker.__main__.run_{command}", return_value=0) as runner:
                self.assertEqual(cli.main([command, target, "--output", "reports",
                                           "--overwrite-original"]), 0)
                self.assertTrue(runner.call_args.args[0].overwrite_original)

    def test_source_command_uses_python_module(self) -> None:
        source = Path("C:/My Books/中文")
        output = Path("C:/My Books Output")
        command = gui.build_batch_command(source, output, frozen=False, dry_run=True)
        self.assertEqual(command[:4], [gui.sys.executable, "-m", "bookmarker", "batch"])
        self.assertEqual(command[4:7], [str(source), "--output", str(output)])
        self.assertNotIn("--engine", command)
        self.assertNotIn("--ocr", command)
        self.assertIn("--dry-run", command)
        checking = gui.build_batch_command(source, output, frozen=False,
                                           verify_existing=True, resume=False)
        self.assertIn("--verify-existing", checking)
        confirming = gui.build_batch_command(source, output, frozen=False, resume=False,
                                             accept_review=source / "一本.pdf")
        index = confirming.index("--accept-review")
        self.assertEqual(confirming[index + 1], str(source / "一本.pdf"))

    def test_removed_engine_argument_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            gui.build_batch_command(Path("input"), Path("output"),
                                    engine="ocr", resume=False, frozen=False)

    def test_source_single_command_uses_file_entry(self) -> None:
        source = Path("C:/My Books/中文 单本.PDF")
        command = gui.build_batch_command(source, Path("C:/Output"), single_file=True,
                                          frozen=False)
        self.assertEqual(command[:4], [gui.sys.executable, "-m", "bookmarker", "single"])
        self.assertEqual(command[4], str(source))

    def test_api_key_only_enters_worker_environment(self) -> None:
        secret = "test-secret-key"
        command = gui.build_batch_command(Path("input"), Path("output"), frozen=False)
        with patch.dict(gui.os.environ, {"DEEPSEEK_API_KEY": "inherited"}):
            deepseek_env = gui.build_batch_environment(api_key=secret)
        self.assertNotIn(secret, " ".join(command))
        self.assertEqual(deepseek_env["DEEPSEEK_API_KEY"], secret)

    def test_missing_deepseek_key_is_reported_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books"
            output = root / "bookmarked"
            source.mkdir()
            errors = io.StringIO()
            with patch.dict(cli.os.environ, {"DEEPSEEK_API_KEY": ""}), redirect_stderr(errors):
                code = cli.main(["batch", str(source), "--output", str(output)])
            self.assertEqual(code, 2)
            self.assertIn("DEEPSEEK_API_KEY", errors.getvalue())
            self.assertFalse(output.exists())
            self.assertFalse(self.data_root.exists())

    def test_skip_bookmarked_needs_no_key_for_bookmarked_pdf_and_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            books = root / "books"
            reports = root / "reports"
            books.mkdir()
            source = books / "one.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=300, height=400)
            writer.add_outline_item("Existing title", 0)
            with source.open("wb") as stream:
                writer.write(stream)
            original = source.read_bytes()
            with patch.dict(cli.os.environ, {"DEEPSEEK_API_KEY": ""}), \
                 patch("bookmarker.deepseek.DeepSeekClient",
                       side_effect=AssertionError("DeepSeek client started")), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main([
                    "batch", str(books), "--output", str(reports),
                    "--skip-bookmarked", "--verify-existing", "--replace-existing",
                    "--overwrite-original",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(reports.exists())
            row = json.loads((self._job_dir(books, reports, overwrite=True) /
                              "bookmarker-report.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(row["status"], "skipped")
            self.assertEqual(row["existing_bookmarks"], 1)
            self.assertEqual(row["toc_pages"], [])
            self.assertTrue(row["options"]["skip_bookmarked"])

    def test_skip_bookmarked_changes_resume_options_and_reaches_deepseek(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            books = root / "books"
            reports = root / "reports"
            books.mkdir()
            source = books / "one.pdf"
            source.touch()
            process = Mock(return_value=BookResult(str(source), "skipped"))
            arguments = ["batch", str(books), "--output", str(reports), "--resume"]
            with mocked_deepseek(process), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(arguments), 0)
                self.assertEqual(cli.main(arguments), 0)
                self.assertEqual(cli.main(arguments + ["--skip-bookmarked"]), 0)
                self.assertEqual(cli.main(arguments + ["--skip-bookmarked"]), 0)
            self.assertEqual(process.call_count, 2)
            self.assertEqual([call.kwargs["skip_bookmarked"] for call in process.call_args_list],
                             [False, True])
            rows = [json.loads(line) for line in (
                self._job_dir(books, reports) / "bookmarker-report.jsonl"
            ).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["options"]["skip_bookmarked"] for row in rows],
                             [False, True])
            self.assertFalse(reports.exists())

    def test_deepseek_report_tracks_model_without_exposing_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books"
            output = root / "bookmarked"
            source.mkdir()
            (source / "sample.pdf").touch()
            fake_deepseek = types.ModuleType("bookmarker.deepseek")
            fake_deepseek.DEEPSEEK_MODEL = "deepseek-test"
            fake_deepseek.PROMPT_VERSION = "test-v1"
            process = Mock(return_value=BookResult(str(source / "sample.pdf"), "dry_run"))
            fake_deepseek.process_book_deepseek = process
            with patch.dict(sys.modules, {"bookmarker.deepseek": fake_deepseek}):
                with patch.dict(cli.os.environ, {"DEEPSEEK_API_KEY": "test-secret-key"}):
                    with redirect_stdout(io.StringIO()):
                        code = cli.main(["batch", str(source), "--output", str(output),
                                         "--dry-run", "--verify-existing"])
            self.assertEqual(code, 0)
            self.assertEqual(process.call_args.kwargs["api_key"], "test-secret-key")
            self.assertTrue(process.call_args.kwargs["verify_existing"])
            self.assertEqual(process.call_args.args[1], output / "sample_deepseek_bookmarked.pdf")
            report = (self._job_dir(source, output) / "bookmarker-report.jsonl").read_text(
                encoding="utf-8")
            row = json.loads(report.strip())
            self.assertEqual(row["options"]["engine"], "deepseek")
            self.assertNotIn("ocr", row["options"])
            self.assertEqual(row["options"]["model"], "deepseek-test")
            self.assertEqual(row["options"]["prompt_version"], "test-v1")
            self.assertGreater(row["options"]["hierarchy_version"], 1)
            self.assertTrue(row["options"]["verify_existing"])
            self.assertNotIn("test-secret-key", report)
            self.assertFalse(output.exists())

    def test_verify_existing_changes_resume_options_and_reaches_deepseek(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books"
            output = root / "bookmarked"
            source.mkdir()
            (source / "sample.pdf").touch()
            process = Mock(return_value=BookResult(str(source / "sample.pdf"), "success"))
            arguments = ["batch", str(source), "--output", str(output), "--resume"]
            with mocked_deepseek(process):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(arguments), 0)
                    output.mkdir()
                    (output / "sample_deepseek_bookmarked.pdf").touch()
                    self.assertEqual(cli.main(arguments), 0)
                    self.assertEqual(cli.main(arguments + ["--verify-existing"]), 0)
            self.assertEqual(process.call_count, 2)
            self.assertTrue(process.call_args.kwargs["verify_existing"])
            rows = [json.loads(line) for line in (
                self._job_dir(source, output) / "bookmarker-report.jsonl"
            ).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["options"]["verify_existing"] for row in rows], [False, True])
            self.assertEqual([path.name for path in output.iterdir()],
                             ["sample_deepseek_bookmarked.pdf"])

    def test_legacy_output_data_moves_before_resume_without_new_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books"
            output = root / "bookmarked"
            source.mkdir()
            pdf = source / "sample.pdf"
            pdf.touch()
            process = Mock(return_value=BookResult(str(pdf), "success"))
            arguments = ["batch", str(source), "--output", str(output), "--resume"]
            with mocked_deepseek(process), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(arguments), 0)
                job_dir = self._job_dir(source, output)
                output.mkdir()
                (output / "sample_deepseek_bookmarked.pdf").touch()
                for name in ("bookmarker-report.jsonl", "bookmarker-summary.csv"):
                    current = job_dir / name
                    (output / name).write_bytes(current.read_bytes())
                    current.unlink()
                old_cache = output / ".bookmarker-cache"
                old_cache.mkdir()
                (old_cache / "recognition.json").write_text("{}", encoding="utf-8")
                self.assertEqual(cli.main(arguments), 0)
            self.assertEqual(process.call_count, 1)
            self.assertEqual([path.name for path in output.iterdir()],
                             ["sample_deepseek_bookmarked.pdf"])
            self.assertTrue((job_dir / "cache" / "recognition.json").is_file())

    def test_cli_log_explains_result_and_reports_token_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books"
            source.mkdir()
            pdf = source / "sample.pdf"
            pdf.touch()
            result = BookResult(str(pdf), "success", toc_pages=[21, 22, 23],
                                entries=[{"title": "第一章"}], api_usage={
                                    "prompt_tokens": 120, "completion_tokens": 30,
                                    "total_tokens": 150, "reported_responses": 1,
                                    "unreported_responses": 0})
            stream = io.StringIO()
            with mocked_deepseek(Mock(return_value=result)), redirect_stdout(stream):
                self.assertEqual(cli.main([
                    "batch", str(source), "--output", str(root / "output")]), 0)
            log = stream.getvalue()
            self.assertIn("印刷目录位于 PDF 第 21 至 23 页", log)
            self.assertIn("输入 120，输出 30，合计 150", log)
            self.assertNotIn("TOC", log)
            self.assertNotIn("entries", log)

    def test_resume_reprocesses_output_from_old_hierarchy_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books"
            output = root / "bookmarked"
            source.mkdir()
            (source / "sample.pdf").touch()
            process = Mock(return_value=BookResult(str(source / "sample.pdf"), "success"))
            arguments = ["batch", str(source), "--output", str(output), "--resume"]
            with mocked_deepseek(process):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(arguments), 0)
                    output.mkdir()
                    (output / "sample_deepseek_bookmarked.pdf").touch()
                    report = self._job_dir(source, output) / "bookmarker-report.jsonl"
                    row = json.loads(report.read_text(encoding="utf-8").strip())
                    row["options"].pop("hierarchy_version")
                    report.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
                    self.assertEqual(cli.main(arguments), 0)
            self.assertEqual(process.call_count, 2)

    def test_deepseek_resume_requires_its_own_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books"
            output = root / "bookmarked"
            source.mkdir()
            (source / "sample.pdf").touch()
            fake_deepseek = types.ModuleType("bookmarker.deepseek")
            fake_deepseek.DEEPSEEK_MODEL = "deepseek-test"
            fake_deepseek.PROMPT_VERSION = "test-v1"
            process = Mock(return_value=BookResult(str(source / "sample.pdf"), "success"))
            fake_deepseek.process_book_deepseek = process
            arguments = ["batch", str(source), "--output", str(output), "--resume"]
            with patch.dict(sys.modules, {"bookmarker.deepseek": fake_deepseek}):
                with patch.dict(cli.os.environ, {"DEEPSEEK_API_KEY": "test-secret-key"}):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.main(arguments), 0)
                        output.mkdir()
                        (output / "sample_bookmarked.pdf").touch()
                        self.assertEqual(cli.main(arguments), 0)
                        (output / "sample_deepseek_bookmarked.pdf").touch()
                        report = self._job_dir(source, output) / "bookmarker-report.jsonl"
                        rows = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
                        rows[-1]["options"]["ocr"] = None
                        report.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                                          encoding="utf-8")
                        self.assertEqual(cli.main(arguments), 0)
            self.assertEqual(process.call_count, 2)

    def test_single_processes_only_selected_pdf_in_same_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            selected = folder / "中文 单本.PDF"
            other = folder / "other.pdf"
            selected.touch()
            other.touch()
            process = Mock(return_value=BookResult(str(selected), "success"))
            with mocked_deepseek(process), redirect_stdout(io.StringIO()):
                code = cli.main(["single", str(selected), "--output", str(folder)])
            self.assertEqual(code, 0)
            process.assert_called_once()
            self.assertEqual(process.call_args.args[0], selected)
            self.assertEqual(process.call_args.args[1], folder / "中文 单本_deepseek_bookmarked.pdf")
            report = (self._job_dir(selected, folder) / "bookmarker-report.jsonl").read_text(
                encoding="utf-8")
            rows = [json.loads(line) for line in report.splitlines()]
            self.assertEqual([row["source"] for row in rows], [str(selected)])

    def test_output_folder_contains_only_generated_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "books" / "one.pdf"
            source.parent.mkdir()
            writer = PdfWriter()
            writer.add_blank_page(width=300, height=400)
            with source.open("wb") as stream:
                writer.write(stream)
            output = root / "bookmarked"
            entry = TocEntry("第一章", 1, "arabic", 1, 1, "第一章 1", 1.0, pdf_page=1)

            def produce_pdf(path: Path, destination: Path, cache: Path,
                            **kwargs: object) -> BookResult:
                pipeline._write_pdf(path, destination, [entry])
                return BookResult(str(path), "success", output=str(destination))

            with mocked_deepseek(Mock(side_effect=produce_pdf)), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["single", str(source), "--output", str(output)]), 0)

            self.assertEqual([path.name for path in output.iterdir()],
                             ["one_deepseek_bookmarked.pdf"])
            self.assertEqual(str(PdfReader(output / "one_deepseek_bookmarked.pdf").outline[0]["/Title"]),
                             "第一章")
            job = self._job_dir(source, output)
            self.assertTrue((job / "bookmarker-report.jsonl").is_file())
            self.assertTrue((job / "bookmarker-summary.csv").is_file())
            self.assertEqual(list((self.data_root / "temp").iterdir()), [])

    def test_overwrite_single_updates_original_and_resume_uses_new_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "中文 原书.pdf"
            reports = folder / "reports"
            writer = PdfWriter()
            writer.add_blank_page(width=300, height=400)
            writer.add_blank_page(width=300, height=400)
            with source.open("wb") as stream:
                writer.write(stream)

            entry = TocEntry("第一章", 1, "arabic", 1, 1, "第一章 1", 1.0, pdf_page=1)

            def produce_pdf(path: Path, output: Path, cache: Path, **kwargs: object) -> BookResult:
                self.assertEqual(path, output)
                pipeline._write_pdf(path, output, [entry])
                return BookResult(str(path), "success", output=str(output))

            arguments = ["single", str(source), "--output", str(reports),
                         "--overwrite-original", "--resume"]
            process = Mock(side_effect=produce_pdf)
            with mocked_deepseek(process):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(arguments), 0)
                    self.assertEqual(cli.main(["single", str(source),
                                               "--overwrite-original", "--resume"]), 0)
            self.assertEqual(process.call_count, 1)
            self.assertEqual(str(PdfReader(source).outline[0]["/Title"]), "第一章")
            self.assertFalse(reports.exists())
            row = json.loads((self._job_dir(source, reports, overwrite=True) /
                              "bookmarker-report.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(row["output"], str(source))
            self.assertTrue(row["options"]["overwrite_original"])
            self.assertEqual((row["source_size"], row["source_mtime_ns"]),
                             (source.stat().st_size, source.stat().st_mtime_ns))
            self.assertEqual({item.name for item in (self.data_root / "temp").iterdir()},
                             {"recovery.lock"})

    def test_overwrite_batch_processes_each_source_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            books = folder / "books"
            reports = folder / "reports"
            books.mkdir()
            for name in ("a.pdf", "b.pdf"):
                writer = PdfWriter()
                writer.add_blank_page(width=300, height=400)
                with (books / name).open("wb") as stream:
                    writer.write(stream)

            entry = TocEntry("序言", 1, "arabic", 1, 1, "序言 1", 1.0, pdf_page=1)

            def produce_pdf(path: Path, output: Path, cache: Path, **kwargs: object) -> BookResult:
                self.assertEqual(path, output)
                pipeline._write_pdf(path, output, [entry])
                return BookResult(str(path), "success", output=str(output))

            arguments = ["batch", str(books), "--output", str(reports),
                         "--overwrite-original", "--resume"]
            process = Mock(side_effect=produce_pdf)
            with mocked_deepseek(process):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(arguments), 0)
                    # The ignored output may even point inside the input tree.
                    self.assertEqual(cli.main(["batch", str(books), "--output",
                                               str(books / "ignored-output"),
                                               "--overwrite-original", "--resume"]), 0)
            self.assertEqual(process.call_count, 2)
            self.assertEqual([str(PdfReader(path).outline[0]["/Title"])
                              for path in sorted(books.glob("*.pdf"))], ["序言", "序言"])
            self.assertFalse(reports.exists())
            self.assertFalse((books / "ignored-output").exists())
            rows = [json.loads(line) for line in (
                self._job_dir(books, reports, overwrite=True) / "bookmarker-report.jsonl"
            ).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["output"] for row in rows],
                             [str(books / "a.pdf"), str(books / "b.pdf")])
            self.assertEqual({item.name for item in (self.data_root / "temp").iterdir()},
                             {"recovery.lock"})

    def test_failed_atomic_overwrite_leaves_original_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "original.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=300, height=400)
            with source.open("wb") as stream:
                writer.write(stream)
            original = source.read_bytes()
            entry = TocEntry("第一章", 1, "arabic", 1, 1, "第一章 1", 1.0, pdf_page=1)
            real_reader = pipeline.PdfReader

            def reject_written_pdf(path: Path, *args: object, **kwargs: object) -> PdfReader:
                if Path(path).suffix == ".partial":
                    raise RuntimeError("PDF verification failed")
                return real_reader(path, *args, **kwargs)

            with patch.object(pipeline, "PdfReader", side_effect=reject_written_pdf):
                with self.assertRaisesRegex(RuntimeError, "verification failed"):
                    pipeline._write_pdf(source, source, [entry])
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(source.with_name(source.name + ".partial").exists())
            self.assertEqual(list((self.data_root / "temp").iterdir()), [])

    def test_single_rejects_missing_or_non_pdf_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            non_pdf = folder / "notes.txt"
            non_pdf.touch()
            output = folder / "output"
            for source in (folder / "missing.pdf", non_pdf):
                with self.subTest(source=source), redirect_stderr(io.StringIO()):
                    code = cli.main(["single", str(source), "--output", str(output)])
                    self.assertEqual(code, 2)
                    self.assertFalse(output.exists())

    def test_frozen_command_uses_sibling_console_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            app = folder / "PDF书签工具.exe"
            worker = folder / gui.WORKER_NAME
            worker.touch()
            with patch.object(gui.sys, "executable", str(app)):
                command = gui.build_batch_command(folder / "输入", folder / "输出", frozen=True)
                single = gui.build_batch_command(folder / "中文 单本.pdf", folder / "输出",
                                                 single_file=True, frozen=True)
        self.assertEqual(command[0], str(worker.resolve()))
        self.assertEqual(command[1], "batch")
        self.assertNotIn("-m", command)
        self.assertEqual(single[0], str(worker.resolve()))
        self.assertEqual(single[1], "single")

    def test_frozen_missing_worker_has_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(gui.sys, "executable", str(Path(temporary) / "PDF书签工具.exe")):
                with self.assertRaisesRegex(FileNotFoundError, gui.WORKER_NAME):
                    gui.build_batch_command(Path("input"), Path("output"), frozen=True)

    def test_frozen_defaults_use_install_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install = Path(temporary) / "portable"
            install.mkdir()
            with patch.object(gui.sys, "executable", str(install / "PDF书签工具.exe")):
                source, output, cwd = gui.default_directories(frozen=True)
                self.assertEqual(source, install)
                self.assertEqual(output, install / "output")
                self.assertEqual(cwd, install)
                self.assertFalse(output.exists())

    def test_source_defaults_find_books_beside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "pdf-bookmarker"
            project.mkdir()
            books = workspace / "books"
            books.mkdir()
            with patch.object(gui, "PROJECT_ROOT", project):
                source, output, cwd = gui.default_directories(frozen=False)
        self.assertEqual(source, books)
        self.assertEqual(output, project / "output")
        self.assertEqual(cwd, project)

    def test_frozen_defaults_do_not_create_input_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install = Path(temporary) / "portable"
            with patch.object(gui.sys, "executable", str(install / "PDF书签工具.exe")):
                source, output, cwd = gui.default_directories(frozen=True)
                self.assertEqual(source, install)
                self.assertEqual(output, install / "output")
                self.assertEqual(cwd, install)
                self.assertFalse(install.exists())


if __name__ == "__main__":
    unittest.main()
