"""One bounded, reusable background filename index across configured sources."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import stat
import threading
import time

from .inventory import _open_directory
from .logs import LogError

MAX_FILES = 100_000
MAX_FOLDERS = 4_000
MAX_DEPTH = 32
MAX_ENTRIES = 200_000
REFRESH_SECONDS = 30


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _empty_skips():
    return {name: 0 for name in ("unreadable", "symlinks", "special", "depth_limit",
                                "folder_limit", "file_limit", "changed", "entry_limit")}


class GlobalFileIndex:
    """Metadata only. Queries share one index and never launch per-query scans."""

    def __init__(self, sources, files, trackers, *, refresh_seconds=REFRESH_SECONDS,
                 max_files=None, max_folders=None, max_depth=None, max_entries=None):
        self.sources = sources
        self.files = files
        self.trackers = trackers
        self.refresh_seconds = refresh_seconds
        self.max_files = MAX_FILES if max_files is None else max_files
        self.max_folders = MAX_FOLDERS if max_folders is None else max_folders
        self.max_depth = MAX_DEPTH if max_depth is None else max_depth
        self.max_entries = MAX_ENTRIES if max_entries is None else max_entries
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="AstroLOG-files")
        self._future = None
        self._running = False
        self._retain_previous = False
        self._finished_at = None
        self._rows = {}
        self._status = "indexing"
        self._skipped = _empty_skips()
        self._truncated = False
        self._updated_at = None
        self._log_ids = {}
        for file_id, spec in files.items():
            relative = spec.path.relative_to(sources[spec.source].root).as_posix()
            self._log_ids.setdefault((spec.source, relative), file_id)

    def close(self):
        self._stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _row(self, source, path, size, mtime):
        return {"name": path.rsplit("/", 1)[-1], "path": path,
                "source": source.id, "source_label": source.label,
                "size_bytes": size, "mtime": mtime, "is_dir": False,
                "log_id": self._log_ids.get((source.id, path))}

    def _seed(self):
        rows = {}
        skips = _empty_skips()
        for file_id, spec in self.files.items():
            if self._stop.is_set():
                break
            try:
                snapshot = self.trackers[file_id].snapshot()
            except (LogError, OSError):
                continue
            if snapshot is None:
                continue
            source = self.sources[spec.source]
            path = spec.path.relative_to(source.root).as_posix()
            key = source.id, path
            if key not in rows and len(rows) >= self.max_files:
                skips["file_limit"] += 1
                break
            rows[key] = self._row(source, path, snapshot.size_bytes, snapshot.mtime)
        return rows, skips

    def _ensure(self):
        with self._lock:
            if self._stop.is_set() or self._running:
                return
            if self._finished_at is not None and time.monotonic() - self._finished_at < self.refresh_seconds:
                return
            self._running = True
            self._status = "indexing"
            self._retain_previous = self._finished_at is not None
        # Seed known logs synchronously once per refresh so an initial search can
        # find registered logs before the remaining directory walk has finished.
        rows, skipped = self._seed()
        with self._lock:
            if self._retain_previous:
                # Preserve the previous searchable snapshot during refresh.
                # Current registered logs get priority without exceeding the cap.
                retained = rows.copy()
                for key, row in self._rows.items():
                    if key not in retained and len(retained) < self.max_files:
                        retained[key] = row
                self._rows = retained
            else:
                self._rows = rows
                self._skipped = skipped
                self._truncated = bool(skipped["file_limit"])
                self._updated_at = _timestamp()
            if self._stop.is_set():
                self._running = False
                return
            self._future = self._executor.submit(self._scan, rows.copy(), skipped.copy())

    def _publish(self, rows, skipped, *, done=False):
        partial = any(skipped[name] for name in ("unreadable", "depth_limit", "folder_limit",
                                                "file_limit", "changed", "entry_limit"))
        truncated = any(skipped[name] for name in ("depth_limit", "folder_limit", "file_limit", "entry_limit"))
        with self._lock:
            if done or not self._retain_previous:
                self._rows = rows.copy()
                self._skipped = skipped.copy()
                self._truncated = truncated
                self._updated_at = _timestamp()
            if done:
                self._running = False
                self._retain_previous = False
                self._finished_at = time.monotonic()
                self._status = "partial" if partial else "ready"

    def _scan(self, rows, skipped):
        queue = deque()
        discovered_folders = 0
        for source in self.sources.values():
            if discovered_folders >= self.max_folders:
                skipped["folder_limit"] += 1
                continue
            queue.append((source, "", 0))
            discovered_folders += 1
        inspected = 0
        last_publish = time.monotonic()
        stop_walk = bool(skipped["file_limit"])
        try:
            while queue and not stop_walk and not self._stop.is_set():
                source, relative, depth = queue.popleft()
                try:
                    descriptor = _open_directory(source, relative)
                except LogError:
                    skipped["unreadable"] += 1
                    continue
                try:
                    with os.scandir(descriptor) as entries:
                        for entry in entries:
                            if self._stop.is_set():
                                break
                            inspected += 1
                            if inspected > self.max_entries:
                                skipped["entry_limit"] += 1
                                stop_walk = True
                                break
                            try:
                                if entry.is_symlink():
                                    skipped["symlinks"] += 1
                                    continue
                                path = f"{relative}/{entry.name}" if relative else entry.name
                                if entry.is_dir(follow_symlinks=False):
                                    if depth >= self.max_depth:
                                        skipped["depth_limit"] += 1
                                    elif discovered_folders >= self.max_folders:
                                        skipped["folder_limit"] += 1
                                    else:
                                        queue.append((source, path, depth + 1))
                                        discovered_folders += 1
                                    continue
                                metadata = entry.stat(follow_symlinks=False)
                                if not stat.S_ISREG(metadata.st_mode):
                                    skipped["special"] += 1
                                    continue
                                key = source.id, path
                                if key not in rows and len(rows) >= self.max_files:
                                    skipped["file_limit"] += 1
                                    stop_walk = True
                                    break
                                rows[key] = self._row(source, path, metadata.st_size, metadata.st_mtime)
                            except FileNotFoundError:
                                skipped["changed"] += 1
                            except OSError:
                                skipped["unreadable"] += 1
                            if time.monotonic() - last_publish >= 0.1:
                                self._publish(rows, skipped)
                                last_publish = time.monotonic()
                except OSError:
                    skipped["unreadable"] += 1
                finally:
                    os.close(descriptor)
        except Exception:
            # Do not expose filesystem paths or leave clients indexing forever.
            skipped["unreadable"] += 1
        finally:
            if self._stop.is_set():
                skipped["changed"] += 1
            self._publish(rows, skipped, done=True)

    def search(self, q="", page=1, page_size=50):
        if not isinstance(q, str) or len(q) > 256:
            raise LogError(422, "q must be a string of at most 256 characters")
        if page < 1 or not 1 <= page_size <= 200:
            raise LogError(422, "page must be positive; page_size must be between 1 and 200")
        self._ensure()
        query = q.strip()
        needle = query.casefold()
        with self._lock:
            rows = list(self._rows.values())
            metadata = {"status": self._status, "indexed_files": len(rows),
                        "updated_at": self._updated_at, "truncated": self._truncated,
                        "skipped": self._skipped.copy()}
        matches = [row for row in rows if needle in row["name"].casefold()]
        matches.sort(key=lambda row: (row["source"].casefold(), row["path"].casefold(), row["path"]))
        start = (page - 1) * page_size
        return {**metadata, "query": query, "files": matches[start:start + page_size],
                "total_count": len(matches), "total_bytes": sum(row["size_bytes"] for row in matches),
                "page": page, "page_size": page_size}
