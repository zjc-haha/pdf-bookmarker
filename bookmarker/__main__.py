"""Command line entry point for one PDF or a folder of PDFs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .storage import (LegacyDataMigrationError, configure_private_temp, data_root, job_data_dir,
                      migrate_legacy_job_data)
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


def _pdf_files(folder: Path, output: Path | None) -> list[Path]:
    output = output.resolve() if output is not None else None
    return sorted(
        (path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"
         and (output is None or output not in path.resolve().parents)),
        key=lambda path: str(path).casefold(),
    )


def _done_files(report: Path) -> dict[str, dict]:
    seen: dict[str, dict] = {}
    try:
        with report.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(row, dict) and isinstance(row.get("source"), str)
                        and row["source"]):
                    seen[row["source"]] = row
    except FileNotFoundError:
        pass
    return seen


def _write_summary(report: Path, destination: Path, paths: list[Path]) -> None:
    latest = _done_files(report)
    columns = ["source", "status", "page_count", "existing_bookmarks", "toc_pages",
               "entry_count", "offsets", "outline_match", "missing_bookmarks",
               "extra_bookmarks", "matched_titles", "wrong_pages", "wrong_levels", "wrong_order",
               "warnings", "error", "output", "processed_at", "prompt_tokens",
               "completion_tokens", "total_tokens", "reported_responses",
               "unreported_responses"]
    with destination.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for path in paths:
            row = latest.get(str(path))
            if row is None:
                continue
            check = row.get("existing_outline_check") or {}
            usage = row.get("api_usage")
            if not isinstance(usage, dict):
                usage = {}
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
                "prompt_tokens": usage.get("prompt_tokens", ""),
                "completion_tokens": usage.get("completion_tokens", ""),
                "total_tokens": usage.get("total_tokens", ""),
                "reported_responses": usage.get("reported_responses", ""),
                "unreported_responses": usage.get("unreported_responses", ""),
            })


def _result_message(status: str, count: int, pages: list[int], note: str) -> str:
    descriptions = {
        "success": f"已生成书签 PDF，写入 {count} 条书签",
        "skipped": "跳过，原 PDF 未改动",
        "dry_run": f"分析完成，识别到 {count} 条目录条目，未修改 PDF",
        "needs_review": "需要人工复核，未修改 PDF",
        "failed": "处理失败，原 PDF 未改动",
    }
    detail = descriptions.get(status, "处理结果已记录")
    if status == "skipped" and count:
        detail += f"；识别到 {count} 条目录条目"
    if pages:
        if len(pages) > 1 and pages == list(range(pages[0], pages[-1] + 1)):
            detail += f"；印刷目录位于 PDF 第 {pages[0]} 至 {pages[-1]} 页"
        else:
            detail += "；印刷目录位于 PDF 第 " + "、".join(map(str, pages)) + " 页"
    if note:
        note = re.sub(r"^[A-Za-z][A-Za-z0-9_]*(?:Error|Exception):\s*", "", note)
        if "Written PDF failed" in note:
            note = "生成的 PDF 校验未通过，原文件未改动"
        elif not re.search(r"[\u4e00-\u9fff]", note):
            note = "详细原因请查看报告"
        detail += f"；{note}"
    return detail


def _run_processing(args: argparse.Namespace, *, single_file: bool) -> int:
    # A previous process may have stopped during a cross-volume overwrite.
    # Resolve that journal before reading or processing any source PDF.
    from .pipeline import recover_pending_overwrites
    try:
        restored = recover_pending_overwrites()
    except (OSError, RuntimeError) as error:
        print(f"无法确认上次覆盖的 PDF 是否完整：{error}", file=sys.stderr)
        return 2
    if restored:
        print(f"已恢复上次中断的 {len(restored)} 本原 PDF。", flush=True)
    input_path = args.input.resolve()
    if args.output is None and not args.overwrite_original:
        print("--output is required when saving bookmarked PDF copies.", file=sys.stderr)
        return 2
    # The output option has no effect in overwrite mode, including validation
    # and job identity.  A local placeholder keeps downstream path types simple.
    output_dir = ((data_root() / "output") if args.overwrite_original else
                  args.output.resolve())
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
        if (not args.overwrite_original and
                (source_dir == output_dir or source_dir in output_dir.parents
                 or output_dir in source_dir.parents)):
            print("Input and output folders must be separate sibling folders, not nested.", file=sys.stderr)
            return 2
    if not args.overwrite_original and output_dir.exists() and not output_dir.is_dir():
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
    job_dir = job_data_dir(input_path, output_dir, args.overwrite_original)
    stop_file = getattr(args, "stop_file", None)
    if stop_file is not None and stop_file.parent.resolve() != job_dir.resolve():
        print("停止标记必须位于软件目录中的本次任务文件夹。", file=sys.stderr)
        return 2
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        print("无法在软件目录保存运行数据；请把免安装版解压到可写入的位置后重试。",
              file=sys.stderr)
        return 2
    try:
        migrated = migrate_legacy_job_data(
            output_dir, job_dir, overwrite_original=args.overwrite_original)
    except LegacyDataMigrationError:
        print("无法整理输出目录中旧版程序留下的报告或缓存；请检查文件是否被占用，"
              "或改用一个空的输出目录。", file=sys.stderr)
        return 2
    if migrated:
        print("已将旧版报告和缓存移到软件目录，输出目录只保留 PDF。", flush=True)
    report = job_dir / "bookmarker-report.jsonl"
    summary = job_dir / "bookmarker-summary.csv"
    done = _done_files(report) if args.resume else {}
    paths = ([input_path] if single_file else
             _pdf_files(source_dir, None if args.overwrite_original else output_dir))
    if limit:
        paths = paths[:limit]
    if single_file:
        print(f"正在处理 PDF：{input_path}", flush=True)
    else:
        print(f"找到 {len(paths)} 本 PDF：{source_dir}", flush=True)
    counts: dict[str, int] = {}
    usage_totals = {key: 0 for key in ("prompt_tokens", "completion_tokens",
                                      "total_tokens", "reported_responses",
                                      "unreported_responses")}
    resume_skipped = 0
    stopped = False
    for index, source in enumerate(paths, 1):
        if stop_file is not None and stop_file.is_file():
            stopped = True
            print("已收到停止请求，当前文件处理完毕；不再开始下一本 PDF。", flush=True)
            break
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
        # Ignore fields retired by newer versions while retaining resume data.
        old_options = {key: value for key, value in old_options.items() if key in options}
        if (old and old.get("source_size") == stat.st_size
                and old.get("source_mtime_ns") == stat.st_mtime_ns
                and old_options == options):
            finished = old.get("status") in {"skipped", "dry_run"} if args.dry_run else old.get("status") in {"success", "skipped"}
            if finished and (args.dry_run or old.get("status") == "skipped" or output.is_file()):
                resume_skipped += 1
                print(f"第 {index}/{len(paths)} 本：{relative}；续跑已完成，本次跳过", flush=True)
                continue
        cache_identity = source if single_file else relative
        fingerprint = hashlib.sha256(str(cache_identity).encode("utf-8")).hexdigest()[:20]
        cache = job_dir / "cache" / fingerprint / f"{stat.st_size:x}-{stat.st_mtime_ns:x}"
        print(f"第 {index}/{len(paths)} 本：{relative}；正在处理", flush=True)
        result = process_book_deepseek(
            source, output, cache, api_key=api_key, front=args.front,
            back=args.back, dry_run=args.dry_run,
            skip_existing=not args.replace_existing,
            verify_existing=args.verify_existing,
            skip_bookmarked=args.skip_bookmarked,
        )
        row = result.as_dict()
        usage = row.get("api_usage")
        if isinstance(usage, dict):
            for key in usage_totals:
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    usage_totals[key] += value
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
        print("处理结果：" + _result_message(
            result.status, len(result.entries), result.toc_pages, note), flush=True)
    labels = {"success": "成功", "skipped": "跳过", "dry_run": "仅分析",
              "needs_review": "需复核", "failed": "失败"}
    summary_parts = [f"{labels.get(key, key)} {value} 本" for key, value in counts.items()]
    if resume_skipped:
        summary_parts.append(f"续跑跳过 {resume_skipped} 本")
    if stopped:
        summary_parts.append("已停止")
    print("本次汇总：" + ("，".join(summary_parts) if summary_parts else "没有待处理的 PDF"),
          flush=True)
    if usage_totals["reported_responses"] or usage_totals["total_tokens"]:
        usage_line = (f"本次已统计 API Token：输入 {usage_totals['prompt_tokens']:,}，"
                      f"输出 {usage_totals['completion_tokens']:,}，"
                      f"合计 {usage_totals['total_tokens']:,}")
    elif counts.get("failed"):
        usage_line = "本次未获取到可统计的 API Token 用量"
    else:
        usage_line = "本次已统计 Token：0（使用已有缓存或跳过）"
    if usage_totals["unreported_responses"]:
        usage_line += f"；另有 {usage_totals['unreported_responses']} 次响应未提供用量"
    if counts.get("failed"):
        usage_line += "；失败请求的用量可能未计入"
    print(usage_line, flush=True)
    _write_summary(report, summary, paths)
    print(f"汇总报告：{summary}", flush=True)
    print(f"详细报告：{report}", flush=True)
    return 130 if stopped else (0 if not counts.get("failed") else 1)


def run_batch(args: argparse.Namespace) -> int:
    return _run_processing(args, single_file=False)


def run_single(args: argparse.Namespace) -> int:
    return _run_processing(args, single_file=True)


def _add_processing_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--output", type=Path,
                         help="Folder for generated PDFs; ignored with --overwrite-original")
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
    command.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)


def main(argv: list[str] | None = None) -> int:
    if getattr(sys, "frozen", False):
        try:
            configure_private_temp()
        except OSError:
            print("软件目录不可写；请将免安装版解压到可写入的文件夹后重试。",
                  file=sys.stderr)
            return 2
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
