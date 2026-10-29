"""Bounded metadata browsing within configured roots, without following links."""

from collections import OrderedDict
from datetime import datetime, timezone
import errno
import fnmatch
import os
from pathlib import PurePosixPath
import stat
import threading
import time

from .config import Source
from .logs import LogError
from .manifest import MANIFESTS

MAX_ENTRIES = 100_000
MAX_CACHE_ROWS = 100_000
MAX_CACHE_FOLDERS = 128


def _relative(path: str) -> str:
    if not isinstance(path, str) or "\x00" in path or "\\" in path:
        raise LogError(403, "Invalid inventory directory")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise LogError(403, "Inventory directory must stay inside its configured source root")
    return "" if str(parsed) == "." else str(parsed)


def _open_directory(source: Source, relative: str) -> int:
    """Use descriptor-relative descent so a replaced directory cannot escape root."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = None
    try:
        descriptor = os.open(source.root, flags)
        for part in PurePosixPath(relative).parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, PermissionError):
            raise LogError(403, "Inventory directory is not readable") from exc
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise LogError(403, "Inventory directory must be a real directory, not a symbolic link") from exc
        if isinstance(exc, FileNotFoundError):
            raise LogError(404, "Inventory directory does not exist") from exc
        raise LogError(400, "Cannot open the configured inventory directory") from exc


class InventoryCache:
    def __init__(self):
        self._cache: OrderedDict[tuple[str, str], tuple] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _pagination(page: int, page_size: int) -> None:
        if page < 1 or not 1 <= page_size <= 200:
            raise LogError(422, "page must be positive; page_size must be between 1 and 200")

    def _scan(self, source: Source, relative: str) -> tuple[list[dict], list[dict]]:
        files, folders = [], []
        try:
            descriptor = _open_directory(source, relative)
        except LogError as exc:
            if exc.status_code == 404 and not relative:
                return files, folders
            raise
        try:
            with os.scandir(descriptor) as entries:
                for number, entry in enumerate(entries, 1):
                    if number > MAX_ENTRIES:
                        raise LogError(413, "Directory has too many entries; configure a narrower source root")
                    try:
                        if entry.is_symlink():
                            continue
                        is_dir = entry.is_dir(follow_symlinks=False)
                        matches = any(fnmatch.fnmatchcase(entry.name, pattern) for pattern in source.inventory_patterns)
                        if not is_dir and not matches:
                            continue
                        metadata = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    path = f"{relative}/{entry.name}" if relative else entry.name
                    if is_dir:
                        folders.append({"name": entry.name, "path": path, "mtime": metadata.st_mtime, "is_dir": True})
                    if matches and (is_dir or stat.S_ISREG(metadata.st_mode)):
                        files.append({"name": entry.name, "path": path, "size_bytes": 0 if is_dir else metadata.st_size,
                                      "mtime": metadata.st_mtime, "is_dir": is_dir})
        except PermissionError as exc:
            raise LogError(403, "Inventory directory is not readable") from exc
        finally:
            os.close(descriptor)
        files.sort(key=lambda item: item["name"])
        folders.sort(key=lambda item: item["name"])
        return files, folders

    def _cached(self, source: Source, relative: str):
        if source.inventory_manifest is not None:
            snapshot = MANIFESTS.get(source.inventory_manifest,
                                     max_age=max(60, source.inventory_refresh_seconds * 3))
            if relative and not any(row["path"] == relative and row["is_dir"] for row in snapshot.entries):
                raise LogError(404, "Inventory directory does not exist")
            entries = snapshot.children.get(relative, ())
            folders = [row for row in entries if row["is_dir"]]
            files = [row for row in entries if any(fnmatch.fnmatchcase(row["name"], pattern)
                                                  for pattern in source.inventory_patterns)]
            return time.monotonic(), files, folders, snapshot.updated_at, snapshot.metadata()
        key = source.id, relative
        with self._lock:
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached is None or now - cached[0] >= source.inventory_refresh_seconds:
                files, folders = self._scan(source, relative)
                cached = (now, files, folders, datetime.now(timezone.utc).isoformat())
                self._cache[key] = cached
            self._cache.move_to_end(key)
            while len(self._cache) > 1 and (len(self._cache) > MAX_CACHE_FOLDERS
                    or sum(len(item[1]) + len(item[2]) for item in self._cache.values()) > MAX_CACHE_ROWS):
                self._cache.popitem(last=False)
            return (*cached, {})

    def page(self, source: Source, page: int = 1, page_size: int = 50, path: str = "", q: str = "") -> dict:
        self._pagination(page, page_size)
        if not isinstance(q, str) or len(q) > 256:
            raise LogError(422, "q must be a string of at most 256 characters")
        relative = _relative(path)
        _, files, _, timestamp, metadata = self._cached(source, relative)
        directory_total_count = len(files)
        query = q.strip()
        if query:
            needle = query.casefold()
            files = [entry for entry in files if needle in entry["name"].casefold()]
        start = (page - 1) * page_size
        return {"source": source.id, "path": relative, "files": files[start:start + page_size],
                "total_count": len(files), "total_bytes": sum(item["size_bytes"] for item in files),
                "directory_total_count": directory_total_count, "query": query,
                "page": page, "page_size": page_size, "updated_at": timestamp, **metadata}

    def folders(self, source: Source, path: str = "", page: int = 1, page_size: int = 50) -> dict:
        self._pagination(page, page_size)
        relative = _relative(path)
        _, _, folders, timestamp, metadata = self._cached(source, relative)
        start = (page - 1) * page_size
        return {"source": source.id, "path": relative, "folders": folders[start:start + page_size],
                "total_count": len(folders), "page": page, "page_size": page_size,
                "updated_at": timestamp, **metadata}
