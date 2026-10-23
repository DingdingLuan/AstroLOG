"""Bounded byte-addressed reads of complete log lines, without modifying inputs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import errno
import os
from pathlib import Path
import stat
import threading
import uuid
from typing import BinaryIO

BLOCK_BYTES = 64 * 1024
MAX_WINDOW_BYTES = 4 * 1024 * 1024
MAX_STREAM_BYTES = 1024 * 1024
MAX_LINES = 2000


class LogError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class FileSnapshot:
    generation: str
    size_bytes: int
    mtime: float


class FileTracker:
    """One configured path. Generation changes on replacement or observed shrink."""

    def __init__(self, path: Path, file_id: str | None = None, allowed_root: Path | None = None):
        self.path = Path(path)
        self.file_id = file_id or str(path)
        self._allowed_root = Path(allowed_root or self.path.parent).resolve()
        # Canonicalize trusted parent aliases once at setup (for example /tmp
        # on macOS). Later reads never resolve a mutable parent symlink.
        self._canonical_path = self.path.parent.resolve() / self.path.name
        self._lock = threading.RLock()
        self._session = uuid.uuid4().hex
        self._identity: tuple[int, int] | None = None
        self._size = 0
        self._epoch = 0
        self._generation = ""

    def _check_path(self) -> tuple[str, ...]:
        # Check the canonical setup path lexically. Resolving it again before
        # opening would leave a race if a parent directory became a symlink.
        absolute = self._canonical_path
        try:
            relative = absolute.relative_to(self._allowed_root).parts
        except ValueError as exc:
            raise LogError(403, "Configured log is outside its allowed source root") from exc
        if not relative:
            raise LogError(400, "Configured log path is not a regular file")
        return relative

    def _open_descriptor(self, flags: int) -> int:
        """Walk real directories by descriptor, including the root's ancestors.

        Each descriptor pins the directory that was checked, so renaming it or
        replacing its old name cannot redirect a later step to another tree.
        """
        relative = self._check_path()
        directory_flags = (getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY
                           | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        descriptor = None
        try:
            descriptor = os.open(self._allowed_root.anchor, directory_flags)
            parts = (*self._allowed_root.parts[1:], *relative[:-1])
            for part in parts:
                child = os.open(part, directory_flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return os.open(relative[-1], flags | getattr(os, "O_NOFOLLOW", 0)
                           | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
                           dir_fd=descriptor)
        except FileNotFoundError as exc:
            self._identity = None
            self._size = 0
            raise LogError(404, "Configured log does not exist yet") from exc
        except PermissionError as exc:
            raise LogError(403, "Configured log is not readable") from exc
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise LogError(403, "Configured log path must not contain symbolic links") from exc
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _observe(self, metadata: os.stat_result) -> FileSnapshot:
        if not stat.S_ISREG(metadata.st_mode):
            raise LogError(400, "Configured log path is not a regular file")
        identity = metadata.st_dev, metadata.st_ino
        if identity != self._identity or metadata.st_size < self._size:
            self._epoch += 1
            token = f"{self._session}:{self.path}:{identity}:{self._epoch}"
            self._generation = hashlib.sha256(token.encode()).hexdigest()[:24]
            self._identity = identity
        self._size = metadata.st_size
        return FileSnapshot(self._generation, metadata.st_size, metadata.st_mtime)

    def snapshot(self) -> FileSnapshot | None:
        with self._lock:
            try:
                # O_PATH obtains metadata without reading the log or requiring
                # read permission on Linux; other POSIX systems use O_RDONLY.
                descriptor = self._open_descriptor(getattr(os, "O_PATH", os.O_RDONLY))
            except LogError as exc:
                if exc.status_code == 404:
                    return None
                raise
            try:
                return self._observe(os.fstat(descriptor))
            finally:
                os.close(descriptor)

    def _open(self) -> tuple[BinaryIO, FileSnapshot]:
        descriptor = self._open_descriptor(os.O_RDONLY)
        try:
            handle = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            return handle, self._observe(os.fstat(handle.fileno()))
        except BaseException:
            handle.close()
            raise

    @staticmethod
    def _complete_end(handle: BinaryIO, size: int) -> int:
        """Find final newline backwards; an unfinished final record is unpublished."""
        position = size
        scanned = 0
        while position > 0 and scanned < MAX_WINDOW_BYTES:
            start = max(0, position - BLOCK_BYTES)
            handle.seek(start)
            chunk = handle.read(position - start)
            found = chunk.rfind(b"\n")
            if found >= 0:
                return start + found + 1
            scanned += len(chunk)
            position = start
        if position:
            raise LogError(413, "An unfinished log line exceeds the 4 MiB read limit")
        return 0

    @staticmethod
    def _window(handle: BinaryIO, end: int, count: int) -> list[dict]:
        if end == 0:
            return []
        chunks: list[bytes] = []
        position = end
        newline_count = 0
        scanned = 0
        while position > 0 and newline_count <= count and scanned < MAX_WINDOW_BYTES:
            start = max(0, position - min(BLOCK_BYTES, MAX_WINDOW_BYTES - scanned))
            handle.seek(start)
            chunk = handle.read(position - start)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
            scanned += len(chunk)
            position = start
        data = b"".join(reversed(chunks))
        origin = position
        if position > 0:
            # The first piece starts in an unknown line; never return a partial UTF-8 record.
            boundary = data.find(b"\n")
            if boundary < 0:
                raise LogError(413, "A log line exceeds the 4 MiB read limit")
            data = data[boundary + 1:]
            origin += boundary + 1
        if not data and end:
            raise LogError(413, "A log line exceeds the 4 MiB read limit")
        # File offsets are newline-delimited; CR-only progress output is left intact.
        records = data.split(b"\n")[:-1]
        records = [record + b"\n" for record in records]
        skipped = max(0, len(records) - count)
        origin += sum(map(len, records[:skipped]))
        return _lines(records[skipped:], origin)

    def open_snapshot(self) -> tuple[BinaryIO, FileSnapshot]:
        """Secure read-only handle plus its starting metadata; caller must close it."""
        with self._lock:
            return self._open()

    def _response(self, snap: FileSnapshot, rows: list[dict], cursor: int,
                  complete_end: int | None = None) -> dict:
        first = rows[0]["start"] if rows else cursor
        end = snap.size_bytes if complete_end is None else complete_end
        return {
            "file_id": self.file_id,
            "generation": snap.generation,
            "lines": rows,
            "cursor": cursor,
            "older_cursor": first,
            "has_more": first > 0,
            "size_bytes": snap.size_bytes,
            "mtime": snap.mtime,
            "newer_cursor": cursor,
            "has_newer": cursor < end,
            "position_percent": round(100 * cursor / snap.size_bytes, 4) if snap.size_bytes else 100.0,
            "start_line": None,
            "end_line": None,
        }

    @staticmethod
    def _validate_count(lines: int) -> None:
        if not 1 <= lines <= MAX_LINES:
            raise LogError(422, f"lines must be between 1 and {MAX_LINES}")

    def tail(self, lines: int = 100) -> dict:
        self._validate_count(lines)
        with self._lock:
            handle, snap = self._open()
            with handle:
                end = self._complete_end(handle, snap.size_bytes)
                rows = self._window(handle, end, lines)
            return self._response(snap, rows, end, end)

    def history(self, before: int, lines: int = 200, generation: str = "") -> dict:
        self._validate_count(lines)
        with self._lock:
            handle, snap = self._open()
            with handle:
                if generation != snap.generation:
                    raise LogError(409, "Log was replaced or truncated; reload its tail")
                if before < 0 or before > snap.size_bytes:
                    raise LogError(422, "History cursor is outside the file")
                if before:
                    handle.seek(before - 1)
                    if handle.read(1) != b"\n":
                        raise LogError(422, "History cursor must be a complete-line boundary")
                rows = self._window(handle, before, lines)
                complete_end = self._complete_end(handle, snap.size_bytes)
            return self._response(snap, rows, before, complete_end)

    @staticmethod
    def _forward_rows(handle: BinaryIO, start: int, complete_end: int, count: int) -> list[dict]:
        handle.seek(start)
        chunks = []
        scanned = 0
        newline_count = 0
        available = min(MAX_WINDOW_BYTES, max(0, complete_end - start))
        while scanned < available and newline_count < count:
            chunk = handle.read(min(BLOCK_BYTES, available - scanned))
            if not chunk:
                break
            chunks.append(chunk)
            scanned += len(chunk)
            newline_count += chunk.count(b"\n")
        data = b"".join(chunks)
        boundary = data.rfind(b"\n")
        if boundary < 0:
            if len(data) == MAX_WINDOW_BYTES:
                raise LogError(413, "A log line exceeds the 4 MiB read limit")
            return []
        records = data[:boundary + 1].split(b"\n", count)
        return _lines([piece + b"\n" for piece in records[:min(count, len(records) - 1)]], start)

    @staticmethod
    def _line_start(handle: BinaryIO, offset: int) -> int:
        position = offset
        scanned = 0
        while position > 0 and scanned < MAX_WINDOW_BYTES:
            start = max(0, position - min(BLOCK_BYTES, MAX_WINDOW_BYTES - scanned))
            handle.seek(start)
            chunk = handle.read(position - start)
            boundary = chunk.rfind(b"\n")
            if boundary >= 0:
                return start + boundary + 1
            scanned += len(chunk)
            position = start
        if position:
            raise LogError(413, "A log line exceeds the 4 MiB read limit")
        return 0

    def forward(self, after: int, lines: int = 200, generation: str = "") -> dict:
        self._validate_count(lines)
        with self._lock:
            handle, snap = self._open()
            with handle:
                if generation != snap.generation:
                    raise LogError(409, "Log was replaced or truncated; reload its tail")
                if after < 0 or after > snap.size_bytes:
                    raise LogError(422, "Forward cursor is outside the file")
                if after:
                    handle.seek(after - 1)
                    if handle.read(1) != b"\n":
                        raise LogError(422, "Forward cursor must be a complete-line boundary")
                complete_end = self._complete_end(handle, snap.size_bytes)
                rows = self._forward_rows(handle, after, complete_end, lines)
            cursor = rows[-1]["end"] if rows else after
            return self._response(snap, rows, cursor, complete_end)

    def centered(self, *, percent: float | None = None, offset: int | None = None,
                 lines: int = 200, generation: str | None = None) -> dict:
        self._validate_count(lines)
        if (percent is None) == (offset is None):
            raise LogError(422, "Specify exactly one target percent or byte offset")
        if percent is not None and not 0 <= percent <= 100:
            raise LogError(422, "percent must be between 0 and 100")
        with self._lock:
            handle, snap = self._open()
            with handle:
                if generation is not None and generation != snap.generation:
                    raise LogError(409, "Log was replaced or truncated; reload its tail")
                complete_end = self._complete_end(handle, snap.size_bytes)
                target = int(snap.size_bytes * percent / 100) if percent is not None else int(offset)
                target = min(max(0, target), complete_end)
                pivot = self._line_start(handle, target)
                older = self._window(handle, pivot, lines // 2) if pivot and lines // 2 else []
                start = older[0]["start"] if older else pivot
                rows = self._forward_rows(handle, start, complete_end, lines)
                if len(rows) < lines and (rows[-1]["end"] if rows else start) == complete_end:
                    rows = self._window(handle, complete_end, lines)
            cursor = rows[-1]["end"] if rows else 0
            result = self._response(snap, rows, cursor, complete_end)
            result["target"] = {"offset": target}
            if percent is not None:
                result["target"]["percent"] = percent
            return result

    def read_new(self, cursor: int, generation: str) -> dict:
        with self._lock:
            handle, snap = self._open()
            with handle:
                if generation != snap.generation or cursor > snap.size_bytes:
                    raise LogError(409, "Log was replaced or truncated; reload its tail")
                if cursor < 0:
                    raise LogError(422, "Cursor must be nonnegative")
                if cursor:
                    handle.seek(cursor - 1)
                    if handle.read(1) != b"\n":
                        raise LogError(422, "Stream cursor must be a complete-line boundary")
                handle.seek(cursor)
                data = handle.read(MAX_STREAM_BYTES)
                boundary = data.rfind(b"\n")
                if boundary < 0:
                    if len(data) == MAX_STREAM_BYTES:
                        raise LogError(413, "A live log line exceeds the 1 MiB read limit")
                    rows = []
                else:
                    complete = data[:boundary + 1]
                    if complete.count(b"\n") > MAX_LINES:
                        pieces = complete.split(b"\n", MAX_LINES)
                        complete = b"\n".join(pieces[:MAX_LINES]) + b"\n"
                    rows = _lines([record + b"\n" for record in complete.split(b"\n")[:-1]], cursor)
                    cursor += len(complete)
            return self._response(snap, rows, cursor)


def _lines(records: list[bytes], origin: int) -> list[dict]:
    result = []
    for record in records:
        end = origin + len(record)
        # Invalid source bytes are visible as replacement characters; offsets stay byte-exact.
        text = record[:-1].removesuffix(b"\r").decode("utf-8", errors="replace")
        result.append({"start": origin, "end": end, "text": text})
        origin = end
    return result
