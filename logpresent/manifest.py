"""Read bounded collector metadata; remote data files are never opened here."""

from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import threading

from .logs import LogError

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_ENTRIES = 20_000
MAX_MANIFEST_CACHE = 128


@dataclass(frozen=True)
class ManifestSnapshot:
    entries: tuple[dict, ...]
    children: dict[str, tuple[dict, ...]]
    updated_at: str
    timestamp: float
    truncated: bool
    skipped: int
    stale: bool = False

    def metadata(self):
        return {"origin": "manifest", "stale": self.stale,
                "truncated": self.truncated, "skipped": self.skipped}


def _read(path: Path, previous_signature=None):
    """Pin each directory and the final regular file, rejecting changed symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path.anchor, flags | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(path.name, flags | getattr(os, "O_NONBLOCK", 0), dir_fd=descriptor)
    finally:
        os.close(descriptor)
    with os.fdopen(file_descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("Inventory manifest must be a bounded regular file")
        signature = (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size)
        if signature == previous_signature:
            return None, signature
        content = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES:
            raise ValueError("Inventory manifest is too large")
    return content, signature


def _parse(content: bytes) -> ManifestSnapshot:
    document = json.loads(content)
    if not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError("Inventory manifest version must be 1")
    updated = document.get("updated_at")
    if not isinstance(updated, str):
        raise ValueError("Inventory manifest requires an ISO timestamp")
    timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Inventory manifest timestamp must include a timezone")
    entries = document.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_MANIFEST_ENTRIES:
        raise ValueError("Inventory manifest has too many entries")
    truncated = document.get("truncated", False)
    skipped = document.get("skipped", 0)
    if not isinstance(truncated, bool) or type(skipped) is not int or skipped < 0:
        raise ValueError("Invalid inventory manifest status")
    rows = {}
    for value in entries:
        if not isinstance(value, dict):
            raise ValueError("Invalid inventory entry")
        raw = value.get("path")
        if (not isinstance(raw, str) or not raw or len(raw) > 4096
                or "\\" in raw or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
            raise ValueError("Invalid inventory entry path")
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or len(path.parts) > 32 or str(path) != raw or raw == ".":
            raise ValueError("Inventory entries must use relative paths")
        name = value.get("name", path.name)
        size = value.get("size_bytes")
        mtime = value.get("mtime")
        is_dir = value.get("is_dir")
        size_known = value.get("size_known", not is_dir)
        if (name != path.name or type(size) is not int or size < 0
                or not isinstance(is_dir, bool) or isinstance(mtime, bool)
                or not isinstance(size_known, bool)
                or not isinstance(mtime, (int, float)) or not math.isfinite(mtime) or mtime < 0
                or raw in rows):
            raise ValueError("Invalid inventory entry metadata")
        rows[raw] = {"path": raw, "name": name, "size_bytes": size,
                     "mtime": mtime, "is_dir": is_dir, "size_known": size_known}
    # Missing parent rows are harmless: synthesize only directory metadata.
    for value in tuple(rows.values()):
        for parent in PurePosixPath(value["path"]).parents:
            if str(parent) == ".":
                continue
            key = str(parent)
            if key in rows and not rows[key]["is_dir"]:
                raise ValueError("Inventory file cannot contain child entries")
            if key not in rows:
                if len(rows) >= MAX_MANIFEST_ENTRIES:
                    raise ValueError("Inventory manifest has too many directories")
                rows[key] = {"path": key, "name": parent.name, "size_bytes": 0,
                             "mtime": value["mtime"], "is_dir": True, "size_known": False}
    children = {}
    for value in rows.values():
        parent = str(PurePosixPath(value["path"]).parent)
        children.setdefault("" if parent == "." else parent, []).append(value)
    return ManifestSnapshot(tuple(rows.values()),
                            {key: tuple(sorted(values, key=lambda row: row["name"]))
                             for key, values in children.items()},
                            updated, timestamp.timestamp(), truncated, skipped)


class ManifestStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = OrderedDict()

    def get(self, path: Path, max_age: float = 90) -> ManifestSnapshot:
        with self._lock:
            previous = self._cache.get(path)
            try:
                content, signature = _read(path, previous[0] if previous is not None else None)
                if previous is not None and previous[0] == signature:
                    snapshot = previous[1]
                else:
                    snapshot = _parse(content)
                    self._cache[path] = signature, snapshot
                self._cache.move_to_end(path)
                while len(self._cache) > MAX_MANIFEST_CACHE:
                    self._cache.popitem(last=False)
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                if previous is None:
                    raise LogError(503, "Collector inventory is not available yet") from None
                return replace(previous[1], stale=True)
            stale = datetime.now(timezone.utc).timestamp() - snapshot.timestamp > max_age
            return replace(snapshot, stale=stale)


MANIFESTS = ManifestStore()
