"""Read-only stdlib agent sent through SSH by ``logpresent.collector``."""

import base64
from collections import deque
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import threading
import time
import uuid

CHUNK_BYTES = 256 * 1024


def relative_path(value, allow_glob=False):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("Expected a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise ValueError("Paths must remain inside the configured root")
    if any(any(c in p for c in "*?[") for p in path.parts[:-1]):
        raise ValueError("Wildcards are supported only in the filename")
    if not allow_glob and any(c in value for c in "*?["):
        raise ValueError("Wildcards are not allowed in local paths")
    if "**" in value:
        raise ValueError("Recursive wildcards are not supported")
    return str(path)


def open_directory(root, relative=""):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(root, flags)
    try:
        for part in PurePosixPath(relative).parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_log(root, relative):
    path = PurePosixPath(relative_path(relative))
    parent = open_directory(root, str(path.parent) if str(path.parent) != "." else "")
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    handle = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        handle.close()
        raise ValueError("Only regular log files can be collected")
    return handle


def select_path(root, specification):
    specification = relative_path(specification, allow_glob=True)
    path = PurePosixPath(specification)
    if not any(c in path.name for c in "*?["):
        return specification
    parent = str(path.parent) if str(path.parent) != "." else ""
    descriptor = open_directory(root, parent)
    latest = None
    try:
        with os.scandir(descriptor) as entries:
            for count, entry in enumerate(entries):
                if count >= 20000:
                    raise ValueError("Log directory has too many entries")
                if entry.is_symlink() or not fnmatch.fnmatchcase(entry.name, path.name):
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(metadata.st_mode):
                    candidate = (metadata.st_mtime_ns, entry.name)
                    if latest is None or candidate > latest:
                        latest = candidate
    finally:
        os.close(descriptor)
    if latest is None:
        raise FileNotFoundError(specification)
    return f"{parent}/{latest[1]}" if parent else latest[1]


def inventory(root, patterns, max_entries=20000, max_depth=4):
    """Collect selected metadata only, never reading snapshot/restart contents."""
    rows = {}
    queue = deque()
    skipped = 0
    truncated = False

    def add(relative, metadata):
        nonlocal truncated
        if relative in rows:
            return True
        if len(rows) >= max_entries:
            truncated = True
            return False
        is_dir = stat.S_ISDIR(metadata.st_mode)
        if not is_dir and not stat.S_ISREG(metadata.st_mode):
            return True
        rows[relative] = {"path": relative, "name": PurePosixPath(relative).name,
                          "size_bytes": 0 if is_dir else metadata.st_size,
                          "mtime": metadata.st_mtime, "is_dir": is_dir,
                          "size_known": not is_dir}
        return True

    for specification in patterns:
        path = PurePosixPath(relative_path(specification, allow_glob=True))
        parent = str(path.parent) if str(path.parent) != "." else ""
        try:
            # Keep ancestor folders visible even before the first snapshot exists.
            current = ""
            for part in PurePosixPath(parent).parts:
                current = f"{current}/{part}" if current else part
                fd = open_directory(root, current)
                try:
                    add(current, os.fstat(fd))
                finally:
                    os.close(fd)
            fd = open_directory(root, parent)
            try:
                with os.scandir(fd) as entries:
                    for number, entry in enumerate(entries):
                        if number >= max_entries * 2:
                            truncated = True
                            break
                        if not fnmatch.fnmatchcase(entry.name, path.name):
                            continue
                        if entry.is_symlink():
                            skipped += 1
                            continue
                        metadata = entry.stat(follow_symlinks=False)
                        relative = f"{parent}/{entry.name}" if parent else entry.name
                        existed = relative in rows
                        if not add(relative, metadata):
                            break
                        if stat.S_ISDIR(metadata.st_mode) and not existed:
                            queue.append((relative, 0))
            finally:
                os.close(fd)
        except (OSError, ValueError):
            skipped += 1

    complete = set()
    while queue and not truncated:
        relative, depth = queue.popleft()
        if depth >= max_depth:
            truncated = True
            continue
        try:
            fd = open_directory(root, relative)
            try:
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            skipped += 1
                            continue
                        metadata = entry.stat(follow_symlinks=False)
                        child = f"{relative}/{entry.name}"
                        if not add(child, metadata):
                            break
                        if stat.S_ISDIR(metadata.st_mode):
                            queue.append((child, depth + 1))
                if not truncated:
                    complete.add(relative)
            finally:
                os.close(fd)
        except OSError:
            skipped += 1

    for relative in sorted(rows, key=lambda value: value.count("/"), reverse=True):
        row = rows[relative]
        if row["is_dir"]:
            row["size_known"] = relative in complete and not truncated and not skipped
        parent = str(PurePosixPath(relative).parent)
        if parent in rows:
            rows[parent]["size_bytes"] += row["size_bytes"]
    return {"version": 1, "updated_at": datetime.now(timezone.utc).isoformat(),
            "entries": sorted(rows.values(), key=lambda row: row["path"]),
            "truncated": truncated, "skipped": skipped}


class Reader:
    def __init__(self, root, logs, resume=None):
        self.root = Path(root).resolve(strict=True)
        self.logs = logs
        self.states = dict(resume or {})

    def poll(self):
        events = []
        for name, specification in self.logs.items():
            try:
                relative = select_path(self.root, specification)
                with open_log(self.root, relative) as handle:
                    metadata = os.fstat(handle.fileno())
                    source_identity = f"{relative}:{metadata.st_dev}:{metadata.st_ino}"
                    state = self.states.get(name, {})
                    offset = state.get("offset", 0)
                    unchanged = state.get("source_identity") == source_identity and metadata.st_size >= offset
                    if unchanged and offset:
                        handle.seek(max(0, offset - 64))
                        anchor = base64.b64encode(handle.read(min(64, offset))).decode("ascii")
                        unchanged = anchor == state.get("anchor")
                    if not unchanged:
                        state = {"source_identity": source_identity, "generation": uuid.uuid4().hex,
                                 "offset": 0, "anchor": "", "remote_path": relative}
                        self.states[name] = state
                        events.append({"event": "reset", "file": name, **state})
                    handle.seek(state["offset"])
                    for _ in range(4):
                        offset = state["offset"]
                        chunk = handle.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        state["offset"] += len(chunk)
                        handle.seek(max(0, state["offset"] - 64))
                        state["anchor"] = base64.b64encode(handle.read(min(64, state["offset"]))).decode("ascii")
                        events.append({"event": "data", "file": name, "offset": offset,
                                       "data": base64.b64encode(chunk).decode("ascii"), **state})
                        # The protocol distinguishes chunk start from persisted cursor.
                        events[-1]["offset"] = offset
                        events[-1]["cursor"] = state["offset"]
                    events.append({"event": "file_status", "file": name, "exists": True,
                                   "remote_path": relative, "size_bytes": metadata.st_size,
                                   "cursor": state["offset"]})
            except FileNotFoundError:
                events.append({"event": "file_status", "file": name, "exists": False})
            except (OSError, ValueError) as exc:
                events.append({"event": "file_status", "file": name, "exists": False,
                               "error": type(exc).__name__})
        return events


def main():
    config = json.loads(sys.argv[1])
    reader = Reader(config["remote_root"], config["logs"], config.get("resume"))
    write_lock = threading.Lock()

    def emit(event):
        with write_lock:
            print(json.dumps(event, separators=(",", ":")), flush=True)

    def metadata_worker():
        while True:
            try:
                manifest = inventory(reader.root, config.get("inventory_patterns", []),
                                     config.get("inventory_max_entries", 20000),
                                     config.get("inventory_max_depth", 4))
                emit({"event": "inventory", "manifest": manifest})
            except Exception as exc:
                emit({"event": "inventory_error", "error": type(exc).__name__})
            time.sleep(config.get("inventory_seconds", 30))

    threading.Thread(target=metadata_worker, daemon=True).start()
    while True:
        for event in reader.poll():
            emit(event)
        emit({"event": "heartbeat", "at": datetime.now(timezone.utc).isoformat()})
        time.sleep(config.get("poll_seconds", 1))


if __name__ == "__main__":
    main()
