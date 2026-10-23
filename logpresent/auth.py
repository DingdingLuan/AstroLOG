"""Optional access-token login with signed, expiring, HTTP-only session cookies."""

from __future__ import annotations

import base64
from collections import OrderedDict, deque
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit

from fastapi import Request

from .config import SecuritySettings
from .logs import LogError

COOKIE_NAME = "logpresent_session"
MAX_TOKEN_LENGTH = 4096
MAX_LOGIN_BYTES = 8192


def _session_secret(token_file) -> bytes:
    """Keep a private random signing secret alongside the token, across restarts.

    The random key prevents a visible signed cookie from becoming an offline
    password-guessing oracle when a user chooses a human-readable access token.
    Atomic hard-link publication avoids partially written keys at startup.
    """
    key_path = token_file.with_name(token_file.name + ".session-key")
    try:
        if not key_path.exists():
            descriptor, temporary_path = tempfile.mkstemp(prefix=".logpresent-session-", dir=key_path.parent)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(secrets.token_bytes(32))
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary_path, key_path)
                except FileExistsError:
                    # Another worker initialized the same persistent key first.
                    pass
            finally:
                os.unlink(temporary_path)
        descriptor = os.open(key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError("Session signing key must be a private regular file with mode 0600")
            secret = handle.read(33)
        if len(secret) != 32:
            raise ValueError("Session signing key must contain exactly 32 random bytes")
        return secret
    except OSError as exc:
        raise ValueError("Cannot initialize or read the private session signing key alongside the access token") from exc


class AuthManager:
    def __init__(self, settings: SecuritySettings):
        self.settings = settings
        self._token = ""
        self._key = b""
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()
        if settings.enabled:
            try:
                with settings.token_file.open("r", encoding="utf-8") as handle:
                    token = handle.read(MAX_TOKEN_LENGTH + 2).strip()
            except OSError as exc:
                raise ValueError("Cannot read security.token_file; initialize the private token file first") from exc
            if not token or len(token) > MAX_TOKEN_LENGTH:
                raise ValueError("The access token file must contain a nonempty token of at most 4096 characters")
            self._token = token
            token_digest = hashlib.sha256(token.encode()).digest()
            self._key = hmac.new(_session_secret(settings.token_file),
                                 b"LogPresent cookie signing key v2\x00" + token_digest,
                                 hashlib.sha256).digest()

    def issue_cookie(self) -> tuple[str, float]:
        issued_at = time.time()
        expires_at = issued_at + self.settings.session_days * 86400
        payload = json.dumps({"v": 1, "iat": issued_at, "exp": expires_at,
                              "nonce": secrets.token_urlsafe(16)}, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self._key, encoded, hashlib.sha256).hexdigest()
        return f"{encoded.decode()}.{signature}", expires_at

    def verify_cookie(self, cookie: str | None) -> float | None:
        if not cookie or len(cookie) > 2048:
            return None
        try:
            encoded, signature = cookie.rsplit(".", 1)
            expected = hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            now = time.time()
            issued_at, expires_at = float(payload["iat"]), float(payload["exp"])
            if (payload.get("v") != 1 or issued_at > now + 30 or expires_at <= now
                    or expires_at - issued_at > self.settings.session_days * 86400 + 1):
                return None
            return expires_at
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
            return None

    def authenticated(self, request: Request) -> bool:
        return not self.settings.enabled or self.verify_cookie(request.cookies.get(COOKIE_NAME)) is not None

    def remaining_seconds(self, request: Request) -> float | None:
        if not self.settings.enabled:
            return None
        expiry = self.verify_cookie(request.cookies.get(COOKIE_NAME))
        return max(0, expiry - time.time()) if expiry is not None else 0

    def check_rate_limit(self, address: str) -> int:
        with self._lock:
            now = time.monotonic()
            failures = self._failures.get(address)
            if failures is None:
                return 0
            while failures and now - failures[0] >= 60:
                failures.popleft()
            self._failures.move_to_end(address)
            if len(failures) >= 5:
                return max(1, int(60 - (now - failures[0])) + 1)
            return 0

    def check_token(self, token: str, address: str) -> bool:
        candidate = token.strip()
        correct = hmac.compare_digest(candidate.encode("utf-8", errors="replace"), self._token.encode("utf-8"))
        with self._lock:
            if correct:
                self._failures.pop(address, None)
            else:
                failures = self._failures.setdefault(address, deque(maxlen=5))
                failures.append(time.monotonic())
                self._failures.move_to_end(address)
                while len(self._failures) > 4096:
                    self._failures.popitem(last=False)
        return correct


def check_origin(request: Request) -> None:
    """Browser POSTs must be same-origin; ordinary CLI clients may omit Origin."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise LogError(403, "Cross-origin requests are not permitted")
    origin = request.headers.get("origin")
    if origin is None:
        return
    try:
        expected = urlsplit(str(request.base_url))
        actual = urlsplit(origin)

        def identity(parsed):
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Invalid origin")
            return parsed.scheme.lower(), parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)

        if actual.path not in ("", "/") or actual.query or actual.fragment or identity(actual) != identity(expected):
            raise ValueError("Different origin")
    except ValueError as exc:
        raise LogError(403, "Cross-origin requests are not permitted") from exc


async def read_login_token(request: Request) -> str:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise LogError(415, "Login requires an application/json body")
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_LOGIN_BYTES:
            raise LogError(413, "Login request is too large")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks))
        token = payload.get("token") if isinstance(payload, dict) else None
    except (ValueError, UnicodeError) as exc:
        raise LogError(400, "Invalid login request") from exc
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise LogError(400, "A nonempty access token of at most 4096 characters is required")
    return token
