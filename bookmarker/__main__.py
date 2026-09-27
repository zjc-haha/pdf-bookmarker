"""Command line entry point for one PDF or a folder of PDFs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .toc import HIERARCHY_VERSION


class _KnownFontBBoxFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.levelno == logging.WARNING
                    and record.getMessage() ==
                    "Could not get FontBBox from font descriptor because None cannot be parsed as 4 floats")


_FONT_BBOX_FILTER = _KnownFontBBoxFilter()


def _configure_pdf_logging() -> None:
    logger = logging.getLogger("pdfminer.pdffont")
    if _FONT_BBOX_FILTER not in logger.filters:
        logger.addFilter(_FONT_BBOX_FILTER)


def _pdf_files(folder: Path, output: Path) -> list[Path]:
    output = output.resolve()
    return sorted(
        (path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"
         and output not in path.resolve().parents),
        key=lambda path: str(path).casefold(),
    )


def _done_files(report: Path) -> dict[str, dict]:
    seen: dict[str, dict] = {}
    if report.exists():
        for line in report.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                seen[row["source"]] = row
            except (ValueError, KeyError):
                continue
    return seen


def _write_summary(report: Path, destination: Path, paths: list[Path]) -> None:
    latest = _done_files(report)
    columns = ["source", "status", "page_count", "existing_bookmarks", "toc_pages",
               "entry_count", "offsets", "outline_match", "missing_bookmarks",
               "extra_bookmarks", "matched_titles", "wrong_pages", "wrong_levels", "wrong_order",
               "warnings", "error", "output", "processed_at"]
    with destination.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for path in paths:
            row = latest.get(str(path))
            if row is None:
                continue
            check = row.get("existing_outline_check") or {}
            writer.writerow({
                "source": row.get("source", ""),
                "status": row.get("status", ""),
                "page_count": row.get("page_count", ""),
                "existing_bookmarks": row.get("existing_bookmarks", ""),
                "toc_pages": ",".join(map(str, row.get("toc_pages", []))),
                "entry_count": len(row.get("entries", [])),
                "offsets": json.dumps(row.get("offsets", {}), ensure_ascii=False),
                "outline_match": check.get("matches", ""),
                "missing_bookmarks": check.get("missing_titles", ""),
                "extra_bookmarks": check.get("unexpected_titles", ""),
                "matched_titles": check.get("matched_titles", ""),
                "wrong_pages": check.get("wrong_pages", ""),
                "wrong_levels": check.get("wrong_levels", ""),
                "wrong_order": check.get("wrong_order", ""),
                "warnings": "; ".join(row.get("warnings", [])),
                "error": row.get("error", ""),
                "output": row.get("output", ""),
                "processed_at": row.get("processed_at", ""),
            })


def _run_processing(args: argparse.Namespace, *, single_file: bool) -> int:
    input_path = args.input.resolve()
    output_dir = args.output.resolve()
    if single_file:
        if not input_path.is_file():
            print(f"Input PDF file not found: {input_path}", file=sys.stderr)
            return 2
        if input_path.suffix.lower() != ".pdf":
            print(f"Input file must be a PDF: {input_path}", file=sys.stderr)
            return 2
        source_dir = input_path.parent
    else:
        source_dir = input_path
        if not source_dir.is_dir():
            print(f"Input folder not found: {source_dir}", file=sys.stderr)
            return 2
        if source_dir == output_dir or source_dir in output_dir.parents or output_dir in source_dir.parents:
            print("Input and output folders must be separate sibling folders, not nested.", file=sys.stderr)
            return 2
    if output_dir.exists() and not output_dir.is_dir():
        print(f"Output must be a folder: {output_dir}", file=sys.stderr)
        return 2
    limit = getattr(args, "limit", 0)
    if args.front < 1 or args.back < 0 or limit < 0:
        print("--front must be positive; --back and --limit cannot be negative.", file=sys.stderr)
        return 2
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key and not args.skip_bookmarked:
        print("DeepSeek API Key is missing. Set the DEEPSEEK_API_KEY environment variable.",
              file=sys.stderr)
        return 2
    from .deepseek import DEEPSEEK_MODEL, PROMPT_VERSION, process_book_deepseek
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / "bookmarker-report.jsonl"
    summary = output_dir / "bookmarker-summary.csv"
    done = _done_files(report) if args.resume else {}
    paths = [input_path] if single_file else _pdf_files(source_dir, output_dir)
    if limit:
        paths = paths[:limit]
    if single_file:
        print(f"Processing PDF file: {input_path}", flush=True)
    else:
        print(f"Found {len(paths)} PDF files in {source_dir}", flush=True)
    counts: dict[str, int] = {}
    for index, source in enumerate(paths, 1):
        stat = source.stat()
        old = done.get(str(source))
        options = {"engine": "deepseek", "model": DEEPSEEK_MODEL, "prompt_version": PROMPT_VERSION,
                   "hierarchy_version": HIERARCHY_VERSION,
                   "front": args.front, "back": args.back,
                   "replace_existing": args.replace_existing,
                   "verify_existing": args.verify_existing,
                   "skip_bookmarked": args.skip_bookmarked,
                   "dry_run": args.dry_run,
                   "overwrite_original": args.overwrite_original}
        relative = source.relative_to(source_dir)
        output = (source if args.overwrite_original else
                  output_dir / relative.parent / (source.stem + "_deepseek_bookmarked.pdf"))
        old_options = old.get("options") if old and isinstance(old.get("options"), dict) else {}
        old_options = {"verify_existing": False, "skip_bookmarked": False,
                       "overwrite_original": False, **old_options}
        # Earlier DeepSeek reports recorded an unused OCR field.
        if old_options.get("engine") == "deepseek":
            old_options.pop("ocr", None)
        if (old and old.get("source_size") == stat.st_size
                and old.get("source_mtime_ns") == stat.st_mtime_ns
                and old_options == options):
            finished = old.get("status") in {"skipped", "dry_run"} if args.dry_run else old.get("status") in {"success", "skipped"}
            if finished and (args.dry_run or old.get("status") == "skipped" or output.is_file()):
                print(f"[{index}/{len(paths)}] resume skip: {source.name}", flush=True)
                continue
        cache_identity = source if single_file else relative
        fingerprint = hashlib.sha256(str(cache_identity).encode("utf-8")).hexdigest()[:20]
        cache = output_dir / ".bookmarker-cache" / fingerprint / f"{stat.st_size:x}-{stat.st_mtime_ns:x}"
        print(f"[{index}/{len(paths)}] {relative}", flush=True)
        result = process_book_deepseek(
            source, output, cache, api_key=api_key, front=args.front,
            back=args.back, dry_run=args.dry_run,
            skip_existing=not args.replace_existing,
            verify_existing=args.verify_existing,
            skip_bookmarked=args.skip_bookmarked,
        )
        row = result.as_dict()
        # In overwrite mode a successful write changes the source itself. Save
        # its new fingerprint so --resume recognizes the completed file.
        recorded_stat = source.stat() if args.overwrite_original and result.status == "success" else stat
        row.update({
            "source_size": recorded_stat.st_size,
            "source_mtime_ns": recorded_stat.st_mtime_ns,
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "options": options,
        })
        with report.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        # Keep a usable report even if the user stops a long batch later.
        _write_summary(report, summary, paths)
        counts[result.status] = counts.get(result.status, 0) + 1
        note = result.error or ("; ".join(result.warnings[:2]) if result.warnings else "")
        check = result.existing_outline_check
        if check and not note:
            matched = check.get("matched_titles", check["expected"] - check["missing_titles"])
            note = ("现有书签与目录一致" if check["matches"] else
                    f"书签与目录不一致：共同标题 {matched}、缺失 {check['missing_titles']}、"
                    f"多出 {check['unexpected_titles']}、"
                    + (f"错页 {check['wrong_pages']}、错层级 {check['wrong_levels']}"
                       if matched else "页码和层级无法比较"))
        print(f"  {result.status}: {len(result.entries)} entries, TOC {result.toc_pages} {note}", flush=True)
    print("Summary: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())), flush=True)
    _write_summary(report, summary, paths)
    print(f"Summary CSV: {summary}", flush=True)
    print(f"Detailed report: {report}", flush=True)
    return 0 if not counts.get("failed") else 1


def run_batch(args: argparse.Namespace) -> int:
    return _run_processing(args, single_file=False)


def run_single(args: argparse.Namespace) -> int:
    return _run_processing(args, single_file=True)


def _add_processing_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--output", type=Path, required=True, help="Output folder")
    command.add_argument("--front", type=int, default=35, help="Maximum front pages to inspect (default: 35)")
    command.add_argument("--back", type=int, default=12, help="Maximum back pages to inspect (default: 12)")
    command.add_argument("--dry-run", action="store_true", help="Analyze and report without writing PDFs")
    command.add_argument("--resume", action="store_true", help="Skip files already completed in the report")
    command.add_argument("--replace-existing", action="store_true", help="Replace existing PDF bookmarks")
    command.add_argument("--skip-bookmarked", action="store_true",
                         help="Skip any PDF with one or more existing bookmarks before recognition")
    command.add_argument("--overwrite-original", action="store_true",
                         help="Write bookmarks directly into each source PDF after verification")
    command.add_argument("--verify-existing", action="store_true",
                         help="Compare existing bookmarks with the printed table of contents and replace mismatches")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments == ["gui"]:
        # The same entry point serves `python -m bookmarker` and both packaged
        # executables. Import Tk only when opening the desktop interface.
        from .gui import main as run_gui

        run_gui()
        return 0

    # The packaged CLI can inherit the Windows code page even when
    # PYTHONIOENCODING is set. The GUI reads its redirected output as UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    _configure_pdf_logging()

    parser = argparse.ArgumentParser(description="Add PDF bookmarks from printed tables of contents.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    batch = subparsers.add_parser("batch", help="Process a folder recursively")
    batch.add_argument("input", type=Path, help="Folder containing source PDFs")
    _add_processing_options(batch)
    batch.add_argument("--limit", type=int, default=0, help="Inspect only the first N PDFs")
    single = subparsers.add_parser("single", help="Process one PDF file")
    single.add_argument("input", type=Path, help="Source PDF file")
    _add_processing_options(single)
    args = parser.parse_args(arguments)
    if args.command == "batch":
        return run_batch(args)
    if args.command == "single":
        return run_single(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
