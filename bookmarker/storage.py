"""Locations for private program data kept beside this installation.

Path helpers do not create directories.  Callers create only the directories
needed for the operation they are about to perform.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_root(*, frozen: bool | None = None, executable: Path | None = None,
              project_root: Path | None = None) -> Path:
    """Return ``data`` beside the EXE, or in the source project root.

    The optional paths make the location testable without writing to the
    actual installation or user profile.
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return Path(executable or sys.executable).resolve().parent / "data"
    return Path(project_root or PROJECT_ROOT).resolve() / "data"


def configure_private_temp() -> Path:
    """Keep bundled process scratch files beside its executable."""
    folder = data_root() / "temp"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = str(folder)
    # tempfile may have resolved the previous environment before this call.
    tempfile.tempdir = None
    return folder


def _path_identity(path: Path) -> str:
    """Normalize a path for a stable job identity on the current platform."""
    return os.path.normcase(os.path.normpath(str(Path(path).resolve())))


def job_data_dir(input_path: Path, output_dir: Path,
                 overwrite_original: bool = False, *, root: Path | None = None) -> Path:
    """Return a deterministic directory for one input/output job.

    In overwrite mode the output folder only holds generated PDFs temporarily
    or is used as a command-line argument; it does not identify the job.
    """
    identity = {"input": _path_identity(input_path),
                "overwrite_original": bool(overwrite_original)}
    if not overwrite_original:
        identity["output"] = _path_identity(output_dir)
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return Path(root) / "jobs" / digest if root is not None else data_root() / "jobs" / digest


class LegacyDataMigrationError(RuntimeError):
    """Old output-folder data could not be moved without risking its contents."""


def _is_redirect(path: Path) -> bool:
    """Include Windows junctions as well as ordinary symbolic links."""
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _regular_file(path: Path) -> bool:
    if _is_redirect(path):
        raise LegacyDataMigrationError(f"Refusing linked data path: {path}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise LegacyDataMigrationError(f"Expected a regular data file: {path}")
    return True


def _directory(path: Path) -> bool:
    if _is_redirect(path):
        raise LegacyDataMigrationError(f"Refusing linked data path: {path}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(mode):
        raise LegacyDataMigrationError(f"Expected a data directory: {path}")
    return True


def _safe_target_parent(job_dir: Path, target: Path) -> None:
    """Reject redirects anywhere under the program's data/jobs directory."""
    try:
        relative = target.relative_to(job_dir)
    except ValueError as error:
        raise LegacyDataMigrationError(f"Target is outside its job directory: {target}") from error
    for directory in (job_dir.parent.parent, job_dir.parent, job_dir):
        if _is_redirect(directory):
            raise LegacyDataMigrationError(f"Refusing linked data directory: {directory}")
    current = job_dir
    for part in relative.parts[:-1]:
        current /= part
        if _is_redirect(current):
            raise LegacyDataMigrationError(f"Refusing linked data directory: {current}")
    if _is_redirect(target):
        raise LegacyDataMigrationError(f"Refusing linked data path: {target}")


def _atomic_bytes(job_dir: Path, target: Path, data: bytes) -> None:
    _safe_target_parent(job_dir, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent,
                                         prefix=f".{target.name}-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _safe_target_parent(job_dir, target)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _jsonl_lines(data: bytes) -> list[bytes]:
    return [line for line in data.split(b"\n") if line]


def _merge_jsonl(legacy: bytes, current: bytes) -> bytes:
    """Put missing old rows before current rows, retaining current precedence."""
    current_lines = _jsonl_lines(current)
    included = Counter(current_lines)
    missing = []
    for line in _jsonl_lines(legacy):
        if included[line]:
            included[line] -= 1
        else:
            missing.append(line)
    return b"".join(line + b"\n" for line in [*missing, *current_lines])


def _summary_rows(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    stream = io.StringIO(data.decode("utf-8-sig"), newline="")
    reader = csv.DictReader(stream)
    columns = list(reader.fieldnames or ())
    if not columns or "source" not in columns:
        raise LegacyDataMigrationError("A summary CSV has no source column")
    rows = list(reader)
    if any(None in row for row in rows):
        raise LegacyDataMigrationError("A summary CSV has extra unnamed columns")
    return columns, rows


def _merge_summary(legacy: bytes, current: bytes) -> bytes:
    if not legacy:
        return current
    if not current:
        return legacy
    old_columns, old_rows = _summary_rows(legacy)
    new_columns, new_rows = _summary_rows(current)
    columns = list(dict.fromkeys([*old_columns, *new_columns]))
    combined: dict[str, dict[str, str]] = {}
    for index, row in enumerate(old_rows):
        combined[row.get("source") or f"__legacy_empty_{index}"] = row
    for index, row in enumerate(new_rows):
        combined[row.get("source") or f"__current_empty_{index}"] = row
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    writer.writerows(combined.values())
    return stream.getvalue().encode("utf-8-sig")


def _cache_inventory(folder: Path) -> tuple[list[Path], list[Path]]:
    """List old cache contents while rejecting links before copying anything."""
    directories = [folder]
    files = []
    for current, names, filenames in os.walk(folder, followlinks=False):
        base = Path(current)
        for name in sorted(names):
            child = base / name
            if not _directory(child):
                raise LegacyDataMigrationError(f"Cache directory vanished: {child}")
            directories.append(child)
        for name in sorted(filenames):
            child = base / name
            if not _regular_file(child):
                raise LegacyDataMigrationError(f"Cache file vanished: {child}")
            files.append(child)
    return directories, files


def _cache_destination(job_dir: Path, relative: Path, data: bytes) -> Path:
    target = job_dir / "cache" / relative
    _safe_target_parent(job_dir, target)
    if not _regular_file(target):
        return target
    if target.read_bytes() == data:
        return target
    # Keep a conflicting old cache entry without overwriting newer data.
    digest = hashlib.sha256(data).hexdigest()
    conflict = (job_dir / "legacy-cache-conflicts" / relative.parent /
                f"{relative.name}.{digest}")
    _safe_target_parent(job_dir, conflict)
    if _regular_file(conflict) and conflict.read_bytes() != data:
        raise LegacyDataMigrationError(f"Conflicting archived cache entry: {conflict}")
    return conflict


def migrate_legacy_job_data(output_dir: Path, job_dir: Path, *,
                            overwrite_original: bool = False) -> tuple[str, ...]:
    """Move old report/cache artifacts out of a PDF output folder.

    Existing job data wins where both locations have a record.  Source files
    are deleted only after all their contents have been copied and verified.
    Repeating an interrupted migration does not duplicate JSONL rows.
    Overwrite mode returns before examining the caller's output path at all.
    """
    if overwrite_original:
        return ()
    output_dir = Path(output_dir)
    job_dir = Path(job_dir)
    try:
        if not _directory(output_dir):
            return ()
        report_old = output_dir / "bookmarker-report.jsonl"
        summary_old = output_dir / "bookmarker-summary.csv"
        cache_old = output_dir / ".bookmarker-cache"
        has_report = _regular_file(report_old)
        has_summary = _regular_file(summary_old)
        has_cache = _directory(cache_old)
        if not (has_report or has_summary or has_cache):
            return ()

        report_new = job_dir / report_old.name
        summary_new = job_dir / summary_old.name
        for target in (report_new, summary_new, job_dir / "cache"):
            _safe_target_parent(job_dir, target)
        if has_report:
            _regular_file(report_new)
        if has_summary:
            _regular_file(summary_new)
        if has_cache and (job_dir / "cache").exists():
            _directory(job_dir / "cache")
        cache_dirs, cache_files = _cache_inventory(cache_old) if has_cache else ([], [])

        old_report = report_old.read_bytes() if has_report else None
        old_summary = summary_old.read_bytes() if has_summary else None
        old_cache = {path: path.read_bytes() for path in cache_files}

        # Finish every destination write before deleting anything in the old
        # output folder.  A failed write leaves the old files usable.
        if old_report is not None:
            current = report_new.read_bytes() if report_new.exists() else b""
            merged = _merge_jsonl(old_report, current)
            if merged != current:
                _atomic_bytes(job_dir, report_new, merged)
        if old_summary is not None:
            current = summary_new.read_bytes() if summary_new.exists() else b""
            merged = _merge_summary(old_summary, current)
            if merged != current:
                _atomic_bytes(job_dir, summary_new, merged)
        if has_cache:
            for folder in cache_dirs:
                target = job_dir / "cache" / folder.relative_to(cache_old)
                _safe_target_parent(job_dir, target / ".directory-check")
                target.mkdir(parents=True, exist_ok=True)
            for source, data in old_cache.items():
                destination = _cache_destination(job_dir, source.relative_to(cache_old), data)
                if not destination.exists():
                    _atomic_bytes(job_dir, destination, data)

        # Verify a source has not changed while copying before removing it.
        if old_report is not None and report_old.read_bytes() != old_report:
            raise LegacyDataMigrationError(f"Old report changed during migration: {report_old}")
        if old_summary is not None and summary_old.read_bytes() != old_summary:
            raise LegacyDataMigrationError(f"Old summary changed during migration: {summary_old}")
        for source, data in old_cache.items():
            if source.read_bytes() != data:
                raise LegacyDataMigrationError(f"Old cache changed during migration: {source}")

        moved = []
        if old_report is not None:
            report_old.unlink()
            moved.append(report_old.name)
        if old_summary is not None:
            summary_old.unlink()
            moved.append(summary_old.name)
        if has_cache:
            for source in old_cache:
                source.unlink()
            for folder in reversed(cache_dirs):
                folder.rmdir()
            moved.append(cache_old.name)
        return tuple(moved)
    except (OSError, UnicodeError, csv.Error) as error:
        raise LegacyDataMigrationError(f"Could not migrate old output data: {error}") from error
