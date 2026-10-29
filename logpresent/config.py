"""Configuration loading. Sources are explicit allowlists, never URL file paths."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import ipaddress
import math
import os
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Announcement:
    path: Path
    title: str = "About this workspace"
    refresh_seconds: float = 15


def _announcement(value: Any, config_directory: Path) -> Announcement | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("announcement must be a mapping with path, title, and refresh_seconds")
    if set(value) - {"path", "title", "refresh_seconds"}:
        raise ValueError("announcement supports only path, title, and refresh_seconds")
    raw_path = value.get("path")
    if (not isinstance(raw_path, str) or not raw_path.strip() or len(raw_path) > 4096
            or any(char in raw_path for char in ("\x00", "\n", "\r"))):
        raise ValueError("announcement.path must be a nonempty file path")
    title = value.get("title", "About this workspace")
    if (not isinstance(title, str) or not title.strip() or len(title) > 200
            or any(char in title for char in ("\n", "\r", "\x00"))):
        raise ValueError("announcement.title must contain between 1 and 200 characters on one line")
    refresh = value.get("refresh_seconds", 15)
    if (isinstance(refresh, bool) or not isinstance(refresh, (int, float))
            or not math.isfinite(refresh) or not 1 <= refresh <= 3600):
        raise ValueError("announcement.refresh_seconds must be between 1 and 3600")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = config_directory / path
    return Announcement(path.resolve(), title.strip(), refresh)


@dataclass(frozen=True)
class LogFile:
    id: str
    source: str
    label: str
    path: Path
    parser: str | None = None
    announcement: Announcement | None = None


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    type: str
    root: Path
    inventory_patterns: tuple[str, ...]
    inventory_refresh_seconds: float
    inventory_manifest: Path | None = None


@dataclass(frozen=True)
class SecuritySettings:
    enabled: bool = False
    token_file: Path | None = None
    session_days: float = 7
    secure_cookie: bool = False


@dataclass(frozen=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8765


def _server(value: Any) -> ServerSettings:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"host", "port"}:
        raise ValueError("server must be a mapping with only host and port")
    host = value.get("host", "127.0.0.1")
    if (not isinstance(host, str) or not host.strip() or len(host) > 253
            or any(char.isspace() for char in host.strip())
            or any(ord(char) < 32 or ord(char) == 127 for char in host)
            or any(char in host for char in "/\\?#[]@")):
        raise ValueError("server.host must be an IP address or hostname without a URL scheme or port")
    host = host.strip()
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError("server.host must be an IP address or hostname without a URL scheme or port") from None
    port = value.get("port", 8765)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("server.port must be an integer between 1 and 65535")
    return ServerSettings(host, port)


def resolve_server(server: ServerSettings, host: str | None = None, port: int | None = None,
                   environ: Mapping[str, str] | None = None) -> ServerSettings:
    """Resolve each bind option: command line, environment, YAML, then defaults."""
    environment = os.environ if environ is None else environ
    resolved_host = host if host is not None else environment.get("LOGPRESENT_HOST") or server.host
    resolved_port = port
    if resolved_port is None:
        environment_port = environment.get("LOGPRESENT_PORT")
        if environment_port:
            if not environment_port.isascii() or not environment_port.isdecimal():
                raise ValueError("LOGPRESENT_PORT must be an integer between 1 and 65535")
            resolved_port = int(environment_port)
        else:
            resolved_port = server.port
    return _server({"host": resolved_host, "port": resolved_port})


@dataclass(frozen=True)
class Settings:
    display: dict[str, Any]
    sources: dict[str, Source]
    files: dict[str, LogFile]
    poll_seconds: float
    security: SecuritySettings
    announcement: Announcement | None = None
    server: ServerSettings = ServerSettings()


def load_config(path: str | Path) -> Settings:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise ValueError("Configuration must be a YAML mapping")
    server = _server(document.get("server"))
    display = dict(document.get("display") or {})
    announcement = _announcement(display.pop("announcement", None), config_path.parent)
    display.setdefault("title", "AstroLOG")
    display.setdefault("subtitle", "A live window into long-running work")
    display.setdefault("project", "Logs")
    server_name = display.get("server_name")
    if server_name is None:
        server_name = ""
    if (not isinstance(server_name, str) or len(server_name) > 200
            or any(char in server_name for char in ("\n", "\r", "\x00"))):
        raise ValueError("display.server_name must be a string of at most 200 characters on one line")
    display["server_name"] = server_name.strip()
    display.setdefault("initial_lines", 100)
    display.setdefault("page_lines", 200)
    display.setdefault("theme", "dark")
    display.setdefault("metrics", [])
    display.setdefault("size_decimals", 4)
    if display["theme"] not in ("dark", "light", "auto"):
        raise ValueError("display.theme must be dark, light, or auto")
    if not isinstance(display["size_decimals"], int) or not 0 <= display["size_decimals"] <= 6:
        raise ValueError("display.size_decimals must be an integer between 0 and 6")
    for name in ("initial_lines", "page_lines"):
        value = display[name]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 2000:
            raise ValueError(f"display.{name} must be an integer between 1 and 2000")
    data = document.get("data") or {}
    poll_seconds = float(data.get("poll_seconds", 0.5))
    if not 0.01 <= poll_seconds <= 60:
        raise ValueError("data.poll_seconds must be between 0.01 and 60")
    source_configs = data.get("sources") or {}
    if not isinstance(source_configs, dict) or not source_configs:
        raise ValueError("Configure at least one data.sources entry")
    sources: dict[str, Source] = {}
    files: dict[str, LogFile] = {}
    for source_id, raw in source_configs.items():
        if not isinstance(source_id, str) or "." in source_id or "/" in source_id:
            raise ValueError("Source IDs cannot contain dots or slashes")
        source_type = raw.get("type", raw.get("source", "local"))
        if source_type != "local":
            raise ValueError(
                f"Source {source_id}: {source_type!r} is not implemented in this demo. "
                "Use a local source; SSH collection is a separate future adapter."
            )
        root = Path(raw["root"]).expanduser()
        if not root.is_absolute():
            root = config_path.parent / root
        root = root.resolve()
        inventory = raw.get("inventory") or {}
        patterns = tuple(inventory.get("patterns") or [])
        if any(not isinstance(p, str) or "/" in p or "\\" in p for p in patterns):
            raise ValueError("Inventory patterns must match names directly inside source.root")
        refresh = float(inventory.get("refresh_seconds", 30))
        if refresh < 0:
            raise ValueError("inventory.refresh_seconds must be nonnegative")
        manifest = inventory.get("manifest")
        if manifest is not None:
            if (not isinstance(manifest, str) or not manifest.strip() or len(manifest) > 4096
                    or any(char in manifest for char in ("\x00", "\n", "\r"))):
                raise ValueError("inventory.manifest must be a nonempty file path")
            manifest = Path(manifest).expanduser()
            if not manifest.is_absolute():
                manifest = config_path.parent / manifest
            manifest = manifest.resolve()
        sources[source_id] = Source(source_id, raw.get("label", source_id), source_type,
                                    root, patterns, refresh, manifest)
        for file_id, value in (raw.get("files") or {}).items():
            if not isinstance(file_id, str) or "." in file_id or "/" in file_id:
                raise ValueError("File IDs cannot contain dots or slashes")
            spec = {"path": value} if isinstance(value, str) else value
            log_path = Path(spec["path"]).expanduser()
            if not log_path.is_absolute():
                log_path = root / log_path
            log_path = log_path.resolve()
            if not log_path.is_relative_to(root):
                raise ValueError(f"File {source_id}.{file_id} is outside its configured source.root")
            key = f"{source_id}.{file_id}"
            files[key] = LogFile(key, source_id, spec.get("label", file_id),
                                 log_path, spec.get("parser"),
                                 _announcement(spec.get("announcement"), config_path.parent))
    if not files:
        raise ValueError("Configure at least one source file")
    display.setdefault("default_file", next(iter(files)))
    if display["default_file"] not in files:
        raise ValueError("display.default_file must name a configured source.file")
    security_raw = document.get("security") or {}
    enabled = security_raw.get("enabled", False)
    secure_cookie = security_raw.get("secure_cookie", False)
    if not isinstance(enabled, bool) or not isinstance(secure_cookie, bool):
        raise ValueError("security.enabled and security.secure_cookie must be true or false")
    session_days = float(security_raw.get("session_days", 7))
    if not 0 < session_days <= 365:
        raise ValueError("security.session_days must be greater than 0 and at most 365")
    token_file = None
    if security_raw.get("token_file"):
        token_file = Path(security_raw["token_file"]).expanduser()
        if not token_file.is_absolute():
            token_file = config_path.parent / token_file
        token_file = token_file.resolve()
    if enabled and token_file is None:
        raise ValueError("security.token_file is required when authentication is enabled")
    security = SecuritySettings(enabled, token_file, session_days, secure_cookie)
    return Settings(display, sources, files, poll_seconds, security, announcement, server)
