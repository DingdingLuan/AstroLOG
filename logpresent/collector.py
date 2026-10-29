"""Mirror explicitly configured logs over a persistent SSH connection."""

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import threading
import time

import yaml

from . import remote_collect

LOGGER = logging.getLogger("astrolog.collector")
SSH_OPTIONS = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3", "-o", "LogLevel=ERROR"]


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def load_collector_config(path):
    path = Path(path).expanduser().resolve()
    with path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("Collector configuration must be a mapping")
    hops = config.get("ssh_hops")
    if (not isinstance(hops, list) or not 1 <= len(hops) <= 4
            or any(not isinstance(hop, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}", hop)
                   for hop in hops)):
        raise ValueError("ssh_hops must contain 1–4 SSH config aliases")
    remote = config.get("remote_root")
    if not isinstance(remote, str) or not remote.startswith("/") or any(ord(c) < 32 for c in remote):
        raise ValueError("remote_root must be an absolute path")
    local = config.get("local_root")
    if not isinstance(local, str) or not local:
        raise ValueError("local_root must be a directory path")
    local = Path(local).expanduser()
    if not local.is_absolute():
        local = path.parent / local
    config["local_root"] = str(local.resolve())
    logs = config.get("logs")
    if not isinstance(logs, dict) or not 1 <= len(logs) <= 64:
        raise ValueError("logs must map 1–64 local filenames to remote paths")
    for name, specification in logs.items():
        remote_collect.relative_path(name)
        remote_collect.relative_path(specification, allow_glob=True)
        if name in ("inventory.json", "collector-status.json", ".collector-state.json"):
            raise ValueError("Collector filenames are reserved")
    patterns = config.setdefault("inventory_patterns", [])
    if not isinstance(patterns, list) or len(patterns) > 64:
        raise ValueError("inventory_patterns must be a list of at most 64 patterns")
    for pattern in patterns:
        remote_collect.relative_path(pattern, allow_glob=True)
    for key, default, lower, upper in (("poll_seconds", 1, .2, 60),
                                       ("inventory_seconds", 30, 5, 3600),
                                       ("inventory_max_entries", 20000, 1, 20000),
                                       ("inventory_max_depth", 4, 1, 16)):
        value = config.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
            raise ValueError(f"{key} must be between {lower} and {upper}")
        if key.startswith("inventory_max_") and not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
    return config


def ssh_command(config, resume):
    remote = {key: value for key, value in config.items() if key not in ("ssh_hops", "local_root")}
    remote["resume"] = resume
    code = Path(remote_collect.__file__).read_text(encoding="utf-8")
    command = shlex.join(["python3", "-u", "-c", code, json.dumps(remote, separators=(",", ":"))])
    for hop in reversed(config["ssh_hops"][1:]):
        command = shlex.join(["ssh", *SSH_OPTIONS, hop, command])
    return ["ssh", *SSH_OPTIONS, config["ssh_hops"][0], command]


class Mirror:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["local_root"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / ".collector-state.json"
        try:
            self.states = json.loads(self.state_path.read_text())
            if not isinstance(self.states, dict):
                self.states = {}
        except (OSError, ValueError):
            self.states = {}
        self.states = {key: value for key, value in self.states.items()
                       if key in config["logs"] and isinstance(value, dict)}
        self.status = {"state": "connecting", "remote_root": config["remote_root"],
                       "connected": False, "files": {}, "last_message_at": None,
                       "updated_at": timestamp(), "last_error": None}
        self._last_status_write = 0
        for name in list(self.states):
            target = self.target(name)
            if not target.is_file() or target.stat().st_size < self.states[name].get("offset", 0):
                del self.states[name]
        self.write_status()

    def target(self, name):
        if name not in self.config["logs"]:
            raise ValueError("Unknown collected file")
        name = remote_collect.relative_path(name)
        result = self.root / name
        current = self.root
        for part in Path(name).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Replica paths cannot contain symbolic links")
        return result

    def save(self):
        atomic_json(self.state_path, self.states)

    def write_status(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_status_write < 0.5:
            return
        self.status["updated_at"] = timestamp()
        atomic_json(self.root / "collector-status.json", self.status)
        self._last_status_write = now

    def accept(self, event):
        kind = event.get("event")
        name = event.get("file")
        self.status.update(connected=True, last_message_at=timestamp(), last_error=None)
        if kind == "reset":
            target = self.target(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".collector-new")
            temporary.write_bytes(b"")
            os.replace(temporary, target)
            self.states[name] = {key: event[key] for key in
                                 ("source_identity", "generation", "offset", "anchor", "remote_path")}
            self.save()
        elif kind == "data":
            target = self.target(name)
            state = self.states.get(name)
            if not state or state["generation"] != event["generation"]:
                raise ValueError("Unexpected log generation")
            offset = event["offset"]
            data = base64.b64decode(event["data"], validate=True)
            if len(data) > remote_collect.CHUNK_BYTES or offset < 0 or offset > state["offset"]:
                raise ValueError("Invalid or non-contiguous log chunk")
            cursor = offset + len(data)
            if cursor != event["cursor"]:
                raise ValueError("Invalid log cursor")
            with target.open("r+b") as handle:
                handle.seek(offset)
                handle.write(data)
                if cursor >= state["offset"]:
                    handle.truncate(cursor)
            if cursor >= state["offset"]:
                state.update(offset=cursor, anchor=event["anchor"])
            self.save()
        elif kind == "inventory":
            manifest = event["manifest"]
            if manifest.get("version") != 1 or not isinstance(manifest.get("entries"), list):
                raise ValueError("Invalid inventory frame")
            atomic_json(self.root / "inventory.json", manifest)
            self.status["inventory_updated_at"] = manifest["updated_at"]
            self.status["inventory_entries"] = len(manifest["entries"])
            self.status["inventory_truncated"] = manifest.get("truncated", False)
        elif kind == "file_status":
            self.target(name)
            self.status["files"][name] = {key: value for key, value in event.items() if key not in ("event", "file")}
        elif kind == "inventory_error":
            self.status["inventory_error"] = event.get("error")
        elif kind != "heartbeat":
            raise ValueError("Unknown collector frame")
        self.status["state"] = "following" if any(
            row.get("exists") for row in self.status["files"].values()) else "waiting_for_logs"
        self.write_status()


def run(config, stop=None):
    stop = stop or threading.Event()
    root = Path(config["local_root"])
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / ".collector.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ValueError("Another collector is already writing this local_root") from None
    mirror = Mirror(config)
    backoff = 1
    process = None
    stderr_tail = bytearray()
    while not stop.is_set():
        try:
            mirror.status.update(state="connecting", connected=False)
            mirror.write_status(force=True)
            process = subprocess.Popen(ssh_command(config, mirror.states), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)

            def stderr_reader(pipe):
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        return
                    stderr_tail.extend(chunk)
                    del stderr_tail[:-8192]

            stderr_tail.clear()
            threading.Thread(target=stderr_reader, args=(process.stderr,), daemon=True).start()
            buffer = bytearray()
            last_message = time.monotonic()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while not stop.is_set():
                    if time.monotonic() - last_message > 90:
                        raise TimeoutError("SSH collector heartbeat timed out")
                    for key, _ in selector.select(1):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            raise ConnectionError("SSH log stream closed")
                        buffer.extend(chunk)
                        if len(buffer) > 32 * 1024 * 1024:
                            raise ValueError("Collector frame exceeds its size limit")
                        while b"\n" in buffer:
                            line, _, remaining = buffer.partition(b"\n")
                            buffer = bytearray(remaining)
                            mirror.accept(json.loads(line))
                            last_message = time.monotonic()
                            backoff = 1
        except (OSError, ValueError, KeyError, TypeError, ConnectionError) as exc:
            error = str(exc)
            if stderr_tail:
                error += ": " + stderr_tail.decode("utf-8", errors="replace")[-2000:].strip()
            LOGGER.warning("Collector disconnected; retry in %ss: %s", backoff, error)
            mirror.status.update(state="reconnecting", connected=False, last_error=error)
            mirror.write_status(force=True)
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            process = None
        if stop.wait(backoff):
            break
        backoff = min(backoff * 2, 30)
    mirror.status.update(state="stopped", connected=False)
    mirror.write_status(force=True)
    lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        config = load_collector_config(args.config)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(config, stop)


if __name__ == "__main__":
    main()
