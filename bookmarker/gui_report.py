"""Read only the JSONL results written during one GUI processing run.

Create :class:`RunReport` immediately before starting the worker.  The
reader remembers the existing report's byte length, then accepts only complete
UTF-8 lines appended after that point.  A replaced or truncated report is read
from its new beginning.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any


_TAIL_SIZE = 128


def _identity(stat: os.stat_result) -> tuple[int, int]:
    return stat.st_dev, stat.st_ino


def _tail(stream, offset: int) -> bytes:
    length = min(offset, _TAIL_SIZE)
    stream.seek(offset - length)
    return stream.read(length)


class RunReport:
    """Track complete report records added since this object was created.

    ``read_new`` may be called while the worker is running and once more
    after it exits.  ``records`` and ``status_counts`` summarize only
    records seen by this reader, with the latest row winning for each source.
    """

    def __init__(self, path: Path,
                 previous: dict[str, dict[str, Any]] | None = None) -> None:
        """``previous`` rows stay in ``records`` until a new row replaces them."""
        self.path = Path(path)
        self._offset = 0
        self._file_identity: tuple[int, int] | None = None
        self._tail = b""
        self._pending = b""
        self._skip_to_newline = False
        self._latest: dict[str, dict[str, Any]] = {}
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                self._offset = stat.st_size
                self._file_identity = _identity(stat)
                self._tail = _tail(stream, self._offset)
                # A pre-existing partial line belongs to an earlier run. Its
                # eventual completion must not be counted as a new record.
                self._skip_to_newline = bool(self._tail and self._tail[-1] != 10)
        except FileNotFoundError:
            pass
        self.start_offset = self._offset
        for source, row in (previous or {}).items():
            self._latest[source] = dict(row)

    @property
    def records(self) -> dict[str, dict[str, Any]]:
        return {source: row.copy() for source, row in self._latest.items()}

    @property
    def latest_by_source(self) -> dict[str, dict[str, Any]]:
        """An explicit alias for ``records``."""
        return self.records

    @property
    def status_counts(self) -> dict[str, int]:
        return dict(Counter(row["status"] for row in self._latest.values()))

    def _reset(self, identity: tuple[int, int]) -> None:
        self._file_identity = identity
        self._offset = 0
        self._tail = b""
        self._pending = b""
        self._skip_to_newline = False
        self._latest.clear()

    def read_new(self) -> tuple[dict[str, Any], ...]:
        """Return newly completed, valid JSONL rows since the previous call.

        An incomplete final line is held until a later call. Malformed JSON or
        rows without string ``source`` and ``status`` fields are ignored.
        """
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = _identity(stat)
                replaced = (self._file_identity is not None
                            and identity != self._file_identity)
                truncated = stat.st_size < self._offset
                rewritten = False
                if not replaced and not truncated and self._tail:
                    rewritten = _tail(stream, self._offset) != self._tail
                if replaced or truncated or rewritten:
                    self._reset(identity)
                else:
                    self._file_identity = identity
                stream.seek(self._offset)
                data = stream.read()
                self._offset = stream.tell()
                self._tail = _tail(stream, self._offset)
        except FileNotFoundError:
            self._file_identity = None
            self._offset = 0
            self._tail = b""
            self._pending = b""
            self._skip_to_newline = False
            self._latest.clear()
            return ()

        if self._skip_to_newline:
            _, separator, data = data.partition(b"\n")
            if not separator:
                return ()
            self._skip_to_newline = False

        lines = (self._pending + data).split(b"\n")
        self._pending = lines.pop()
        new_rows: list[dict[str, Any]] = []
        for raw_line in lines:
            try:
                row = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if (not isinstance(row, dict)
                    or not isinstance(row.get("source"), str)
                    or not row["source"]
                    or not isinstance(row.get("status"), str)
                    or not row["status"]):
                continue
            self._latest[row["source"]] = row
            new_rows.append(row)
        return tuple(new_rows)

    def read_new_rows(self) -> tuple[dict[str, Any], ...]:
        """An explicit alias for ``read_new``."""
        return self.read_new()
