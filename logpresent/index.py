"""Incremental sparse newline index; bounded chunks, no retained log text."""

from __future__ import annotations

from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
import threading

from .logs import FileTracker, LogError

ANCHOR_LINES = 256
INDEX_CHUNK_BYTES = 1024 * 1024
MAX_LOOKUP_BYTES = 16 * 1024 * 1024


class SparseLineIndex:
    def __init__(self, tracker: FileTracker, executor: ThreadPoolExecutor):
        self.tracker = tracker
        self.executor = executor
        self._lock = threading.Lock()
        self._generation = None
        self._scan_offset = 0
        self._complete_lines = 0
        self._anchors = [(1, 0)]
        self._future = None
        self._stopped = False
        self._error = None

    def stop(self):
        self._stopped = True

    def _sync(self, generation):
        if generation != self._generation:
            self._generation = generation
            self._scan_offset = 0
            self._complete_lines = 0
            self._anchors = [(1, 0)]
            self._error = None

    def status(self) -> dict:
        snap = self.tracker.snapshot()
        with self._lock:
            self._sync(snap.generation if snap else None)
            size = snap.size_bytes if snap else 0
            ready = snap is not None and self._scan_offset >= size
            if snap and not ready and not self._stopped and (self._future is None or self._future.done()):
                self._future = self.executor.submit(self._build, snap.generation)
            return {"generation": self._generation,
                    "status": "missing" if snap is None else ("error" if self._error else "ready" if ready else "indexing"),
                    "progress": min(1.0, self._scan_offset / size) if size else (1.0 if snap else 0.0),
                    "indexed_bytes": self._scan_offset, "total_bytes": size,
                    "total_complete_lines": self._complete_lines, "ready": ready and not self._error}

    def _build(self, generation):
        try:
            handle, snap = self.tracker.open_snapshot()
            with handle:
                if snap.generation != generation:
                    return
                while not self._stopped:
                    current = self.tracker.snapshot()
                    if current is None or current.generation != generation:
                        return
                    with self._lock:
                        if self._generation != generation:
                            return
                        start = self._scan_offset
                        count = self._complete_lines
                    if start >= current.size_bytes:
                        return
                    handle.seek(start)
                    chunk = handle.read(min(INDEX_CHUNK_BYTES, current.size_bytes - start))
                    if not chunk:
                        return
                    anchors = []
                    position = 0
                    while True:
                        boundary = chunk.find(b"\n", position)
                        if boundary < 0:
                            break
                        count += 1
                        if count % ANCHOR_LINES == 0:
                            anchors.append((count + 1, start + boundary + 1))
                        position = boundary + 1
                    # Recheck identity outside the index lock, avoiding lock inversion with the tracker.
                    after = self.tracker.snapshot()
                    if after is None or after.generation != generation:
                        return
                    with self._lock:
                        if self._generation != generation:
                            return
                        self._scan_offset = start + len(chunk)
                        self._complete_lines = count
                        self._anchors.extend(anchors)
        except (OSError, LogError):
            with self._lock:
                if self._generation == generation:
                    self._error = "Unable to read the configured log for indexing"

    def offset_for_line(self, line: int, generation: str) -> int:
        state = self.status()
        if state["generation"] != generation:
            raise LogError(409, "Log was replaced or truncated; reload its tail")
        if not state["ready"]:
            raise LogError(202, "Line index is still being built")
        if line < 1 or line > state["total_complete_lines"]:
            raise LogError(416, "Requested line is outside the complete log")
        with self._lock:
            anchor_lines = [anchor[0] for anchor in self._anchors]
            anchor_line, offset = self._anchors[max(0, bisect_right(anchor_lines, line) - 1)]
        if anchor_line == line:
            return offset
        handle, snap = self.tracker.open_snapshot()
        with handle:
            if snap.generation != generation:
                raise LogError(409, "Log was replaced or truncated; reload its tail")
            handle.seek(offset)
            remaining = line - anchor_line
            scanned = 0
            while remaining > 0 and scanned < MAX_LOOKUP_BYTES:
                chunk = handle.read(min(64 * 1024, MAX_LOOKUP_BYTES - scanned))
                if not chunk:
                    break
                position = 0
                while remaining > 0:
                    boundary = chunk.find(b"\n", position)
                    if boundary < 0:
                        break
                    remaining -= 1
                    position = boundary + 1
                    if remaining == 0:
                        return offset + scanned + position
                scanned += len(chunk)
        raise LogError(413, "Sparse line lookup exceeds the 16 MiB bounded read limit")

    def line_at_offset(self, offset: int, generation: str) -> int | None:
        with self._lock:
            if generation != self._generation or offset > self._scan_offset:
                return None
            anchor_offsets = [anchor[1] for anchor in self._anchors]
            anchor_line, start = self._anchors[max(0, bisect_right(anchor_offsets, offset) - 1)]
        if offset == start:
            return anchor_line
        if offset - start > MAX_LOOKUP_BYTES:
            return None
        handle, snap = self.tracker.open_snapshot()
        with handle:
            if snap.generation != generation:
                return None
            handle.seek(start)
            count = 0
            remaining = offset - start
            while remaining:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    return None
                count += chunk.count(b"\n")
                remaining -= len(chunk)
        return anchor_line + count

    def decorate(self, window: dict) -> dict:
        rows = window["lines"]
        if not rows:
            return window
        start_line = self.line_at_offset(rows[0]["start"], window["generation"])
        if start_line is not None:
            window["start_line"] = start_line
            window["end_line"] = start_line + len(rows) - 1
            for number, row in enumerate(rows, start_line):
                row["line_number"] = number
        return window
