"""Mirror explicitly configured logs over a persistent SSH connection."""

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import contextlib
import glob
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shlex
import signal
import time

import asyncssh
from filelock import FileLock, Timeout as LockTimeout
import yaml

from . import remote_collect

LOGGER = logging.getLogger("astrolog.collector")
CONNECT_TIMEOUT = 15
LOGIN_TIMEOUT = 20
HEARTBEAT_TIMEOUT = 90
RETRY_INITIAL = 1
RETRY_MAX = 30
CLOSE_TIMEOUT = 5
MAX_FRAME_BYTES = 32 * 1024 * 1024


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def _ssh_config(value, directory):
    if not isinstance(value, dict) or set(value) - {
            "host", "user", "port", "identity_file", "certificate_file", "known_hosts"}:
        raise ValueError("ssh must contain host and optional user, port, identity_file, certificate_file, known_hosts")
    host = value.get("host")
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ValueError("ssh.host must be a hostname or IP address")
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError("ssh.host must be a hostname or IP address") from None
        if "%" in host and not re.fullmatch(r"[A-Za-z0-9_.-]+", host.split("%", 1)[1]):
            raise ValueError("ssh.host has an invalid IPv6 scope")
    elif not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}", host):
        raise ValueError("ssh.host must be a hostname or IP address")
    connection = {"host": host, "port": value.get("port", 22)}
    if type(connection["port"]) is not int or not 1 <= connection["port"] <= 65535:
        raise ValueError("ssh.port must be an integer between 1 and 65535")
    if "user" in value:
        user = value["user"]
        if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@+\-$]{0,199}", user):
            raise ValueError("ssh.user must be a nonempty SSH username")
        connection["user"] = user
    for key in ("identity_file", "certificate_file", "known_hosts"):
        if key not in value:
            continue
        raw = value[key]
        if (not isinstance(raw, str) or not raw.strip() or len(raw) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
            raise ValueError(f"ssh.{key} must be a readable file path")
        file = Path(raw).expanduser()
        if not file.is_absolute():
            file = directory / file
        file = file.resolve()
        if not file.is_file() or not os.access(file, os.R_OK):
            raise ValueError(f"ssh.{key} must name an existing readable file")
        connection[key] = str(file)
    return connection


def load_collector_config(path):
    path = Path(path).expanduser().resolve()
    with path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("Collector configuration must be a mapping")
    if "ssh" in config:
        if "ssh_hops" in config:
            raise ValueError("Configure either ssh or ssh_hops, not both")
        config["ssh"] = _ssh_config(config["ssh"], path.parent)
    else:
        hops = config.get("ssh_hops")
        if (not isinstance(hops, list) or not 1 <= len(hops) <= 4
                or any(not isinstance(hop, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}", hop)
                       for hop in hops)):
            raise ValueError("ssh_hops must contain 1–4 SSH config aliases, or configure ssh directly")
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


def build_remote_command(config, resume):
    """Build only the remote reader command; credentials never go to the server."""
    remote = {key: value for key, value in config.items()
              if key not in ("ssh", "ssh_hops", "local_root")}
    remote["resume"] = resume
    code = Path(remote_collect.__file__).read_text(encoding="utf-8")
    return shlex.join(["python3", "-u", "-c", code,
                       json.dumps(remote, separators=(",", ":"))])


def _checked_ssh_config(path, seen=None):
    """Reject SSH config hooks which could execute external commands."""
    seen = set() if seen is None else seen
    path = Path(path).expanduser().resolve()
    if path in seen or not path.exists():
        return
    if len(seen) >= 32 or path.stat().st_size > 1024 * 1024:
        raise ValueError("SSH alias configuration exceeds its size limit; configure ssh directly")
    seen.add(path)
    for line in path.read_text(encoding="utf-8").splitlines():
        words = shlex.split(line, comments=True)
        if not words:
            continue
        name = words[0].lower()
        if "=" in name:
            name, first = name.split("=", 1)
            words = [name, first, *words[1:]]
        if name in ("proxycommand", "localcommand", "knownhostscommand"):
            if len(words) > 1 and words[1].lower() != "none":
                raise ValueError(f"SSH alias {name} is not supported; configure ssh directly")
        if name == "match" and any(word.lower().lstrip("!") == "exec" for word in words[1:]):
            raise ValueError("SSH Match exec is not supported; configure ssh directly")
        if name == "include":
            for pattern in words[1:]:
                pattern = Path(pattern).expanduser()
                if not pattern.is_absolute():
                    pattern = Path.home() / ".ssh" / pattern
                for included in glob.iglob(str(pattern), recursive=True):
                    if Path(included).is_file():
                        _checked_ssh_config(included, seen)


def connection_options(config):
    """Native AsyncSSH options, with strict host verification by default."""
    common = {"connect_timeout": CONNECT_TIMEOUT, "login_timeout": LOGIN_TIMEOUT,
              "keepalive_interval": 15, "keepalive_count_max": 3,
              "proxy_command": None}
    if "ssh" in config:
        connection = config["ssh"]
        options = {**common, "host": connection["host"], "port": connection["port"], "config": []}
        if "user" in connection:
            options["username"] = connection["user"]
        if "identity_file" in connection:
            options["client_keys"] = [connection["identity_file"]]
        if "certificate_file" in connection:
            options["client_certs"] = [connection["certificate_file"]]
        if "known_hosts" in connection:
            options["known_hosts"] = connection["known_hosts"]
        return [options]
    config_path = Path.home() / ".ssh" / "config"
    _checked_ssh_config(config_path)
    paths = [str(config_path)] if config_path.exists() else []
    result = []
    for alias in config["ssh_hops"]:
        options = asyncssh.SSHClientConnectionOptions(host=alias, config=paths, **common)
        if options.tunnel:
            raise ValueError("SSH alias ProxyJump is not supported here; list the aliases in ssh_hops")
        result.append({"host": alias, "options": options, **common})
    return result


async def _close_connections(connections):
    for connection in reversed(connections):
        connection.close()
        try:
            await asyncio.wait_for(connection.wait_closed(), CLOSE_TIMEOUT)
        except (asyncio.TimeoutError, OSError, asyncssh.Error):
            connection.abort()


async def open_connections(config):
    connections = []
    try:
        for options in connection_options(config):
            arguments = dict(options)
            # Hops use SSH forwarding channels. Every key is local to the collector.
            arguments["tunnel"] = connections[-1] if connections else None
            connection = await asyncio.wait_for(asyncssh.connect(**arguments),
                                                CONNECT_TIMEOUT + LOGIN_TIMEOUT)
            connections.append(connection)
        return connections
    except BaseException:
        await _close_connections(connections)
        raise


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


async def _collect_session(config, mirror, on_message):
    connections = []
    process = None
    stderr_task = None
    stderr_tail = bytearray()
    try:
        connections = await open_connections(config)
        process = await connections[-1].create_process(
            build_remote_command(config, mirror.states), encoding=None, stdin=asyncssh.DEVNULL)

        async def drain_stderr():
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    return
                stderr_tail.extend(chunk)
                del stderr_tail[:-8192]

        stderr_task = asyncio.create_task(drain_stderr())
        buffer = bytearray()
        while True:
            chunk = await asyncio.wait_for(process.stdout.read(65536), HEARTBEAT_TIMEOUT)
            if not chunk:
                raise ConnectionError("SSH log stream closed")
            buffer.extend(chunk)
            if len(buffer) > MAX_FRAME_BYTES:
                raise ValueError("Collector frame exceeds its size limit")
            while b"\n" in buffer:
                line, _, remaining = buffer.partition(b"\n")
                buffer = bytearray(remaining)
                mirror.accept(json.loads(line))
                on_message()
    except (asyncio.TimeoutError, OSError, ValueError, KeyError, TypeError, asyncssh.Error) as exc:
        if stderr_tail:
            raise ConnectionError(str(exc) + ": " +
                                  stderr_tail.decode("utf-8", errors="replace")[-2000:].strip()) from exc
        raise
    finally:
        if stderr_task is not None:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError, asyncssh.Error):
                await stderr_task
        if process is not None:
            process.close()
            with contextlib.suppress(asyncio.TimeoutError, OSError, asyncssh.Error):
                await asyncio.wait_for(process.wait_closed(), CLOSE_TIMEOUT)
        await _close_connections(connections)


def _write_status(mirror):
    try:
        mirror.write_status(force=True)
    except OSError as exc:
        # A transient NFS outage must not kill the reconnect loop.
        LOGGER.warning("Cannot write collector status: %s", exc)


async def _wait_for_stop(stop):
    while not stop.is_set():
        await asyncio.sleep(0.1)


async def run_async(config, stop=None):
    """Collect with native AsyncSSH, independent of any system ssh executable."""
    stop = stop if stop is not None else asyncio.Event()
    root = Path(config["local_root"])
    root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(root / ".collector.lock"))
    try:
        lock.acquire(timeout=0)
    except LockTimeout:
        raise ValueError("Another collector is already writing this local_root") from None
    mirror = None
    stopper = None
    backoff = RETRY_INITIAL
    try:
        mirror = Mirror(config)
        mirror.status["transport"] = "asyncssh"
        stopper = asyncio.create_task(_wait_for_stop(stop))
        while not stop.is_set():
            mirror.status.update(state="connecting", connected=False)
            _write_status(mirror)

            def received():
                nonlocal backoff
                backoff = RETRY_INITIAL

            session = asyncio.create_task(_collect_session(config, mirror, received))
            try:
                done, _ = await asyncio.wait((session, stopper), return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    session.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await session
                    break
                await session
            except (asyncio.TimeoutError, OSError, ValueError, KeyError, TypeError, asyncssh.Error) as exc:
                LOGGER.warning("Collector disconnected; retry in %ss: %s", backoff, exc)
                mirror.status.update(state="reconnecting", connected=False, last_error=str(exc))
                _write_status(mirror)
            finally:
                if not session.done():
                    session.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await session
            if not stop.is_set():
                await asyncio.wait((stopper,), timeout=backoff)
                backoff = min(backoff * 2, RETRY_MAX)
    finally:
        if stopper is not None:
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
        if mirror is not None:
            mirror.status.update(state="stopped", connected=False)
            _write_status(mirror)
        lock.release()


def run(config, stop=None):
    """Synchronous entry point for scripts and existing integrations."""
    asyncio.run(run_async(config, stop))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        config = load_collector_config(args.config)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # AsyncSSH INFO logs include the complete remote command and inline reader.
    logging.getLogger("asyncssh").setLevel(logging.WARNING)

    async def main_async():
        stop = asyncio.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        await run_async(config, stop)

    try:
        asyncio.run(main_async())
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
