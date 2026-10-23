"""HTTP API, static UI, and resumable SSE over configured local files."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
from pathlib import Path
import time
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import __version__
from .auth import AuthManager, COOKIE_NAME, check_origin, read_login_token
from .announcements import read_announcement
from .config import load_config
from .downloads import download_response
from .inventory import InventoryCache
from .index import SparseLineIndex
from .logs import FileTracker, LogError
from .parsers import generic_summary, parse_transfer_status
from .search import GlobalFileIndex


def _sse(event: str, data: dict, event_id: str | None = None) -> dict:
    result = {"event": event, "data": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}
    if event_id is not None:
        result["id"] = event_id
    return result


async def stream_events(tracker: FileTracker, cursor: int, generation: str,
                        poll_seconds: float = 0.5, request: Request | None = None,
                        expires_at: float | None = None) -> AsyncIterator[dict]:
    """Library supplies heartbeat comments; this iterator supplies application events."""
    previous_status = None
    last_status_at = 0.0
    while True:
        if expires_at is not None and time.time() >= expires_at:
            yield _sse("auth_expired", {"authenticated": False})
            return
        if request is not None and await request.is_disconnected():
            return
        try:
            snap = await asyncio.to_thread(tracker.snapshot)
            if snap is None:
                status = {"exists": False, "connected": True, "size_bytes": 0, "mtime": None}
            else:
                if snap.generation != generation or cursor > snap.size_bytes:
                    yield _sse("reset", {"file_id": tracker.file_id, "generation": snap.generation,
                                         "reason": "replaced_or_truncated"})
                    return
                result = await asyncio.to_thread(tracker.read_new, cursor, generation)
                status = {"exists": True, "connected": True,
                          "size_bytes": result["size_bytes"], "mtime": result["mtime"]}
                if result["lines"]:
                    cursor = result["cursor"]
                    yield _sse("append", {key: result[key] for key in ("file_id", "generation", "lines", "cursor")},
                               f"{generation}:{cursor}")
            now = time.monotonic()
            # Status follows size changes; idle check every 15 s is separate from SSE heartbeat.
            if status != previous_status or now - last_status_at >= 15:
                yield _sse("status", status)
                previous_status = status
                last_status_at = now
        except LogError as exc:
            if exc.status_code == 409:
                snap = await asyncio.to_thread(tracker.snapshot)
                yield _sse("reset", {"file_id": tracker.file_id,
                                     "generation": snap.generation if snap else None,
                                     "reason": "replaced_or_truncated"})
                return
            if exc.status_code == 404:
                yield _sse("status", {"exists": False, "connected": True, "size_bytes": 0, "mtime": None})
            else:
                yield _sse("error", {"detail": exc.detail, "status_code": exc.status_code})
                return
        delay = poll_seconds if expires_at is None else min(poll_seconds, max(0, expires_at - time.time()))
        await asyncio.sleep(delay)


def create_app(config_path: str | Path = "config/config.yaml") -> FastAPI:
    settings = load_config(config_path)
    trackers = {key: FileTracker(value.path, key, settings.sources[value.source].root)
                for key, value in settings.files.items()}
    inventory = InventoryCache()
    auth = AuthManager(settings.security)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="LogPresent-index")
    indexes = {key: SparseLineIndex(tracker, executor) for key, tracker in trackers.items()}
    search_index = GlobalFileIndex(settings.sources, settings.files, trackers)
    started_at = time.monotonic()

    @asynccontextmanager
    async def lifespan(application):
        yield
        for item in indexes.values():
            item.stop()
        search_index.close()
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="AstroLOG", version=__version__, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.trackers = trackers
    app.state.auth = auth
    app.state.indexes = indexes
    app.state.search_index = search_index

    @app.exception_handler(LogError)
    async def log_error_handler(request: Request, exc: LogError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.middleware("http")
    async def read_only_headers(request: Request, call_next):
        public_auth = {"/api/auth/status", "/api/auth/login", "/api/auth/logout"}
        if (settings.security.enabled and request.url.path.startswith("/api/")
                and request.url.path not in public_auth and not auth.authenticated(request)):
            return JSONResponse(status_code=401, content={"detail": "Authentication required"},
                                headers={"Cache-Control": "no-store", "Vary": "Cookie"})
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Cookie"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def tracker_for(file_id: str) -> FileTracker:
        tracker = trackers.get(file_id)
        if tracker is None:
            raise HTTPException(404, "Unknown configured file ID")
        return tracker

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        return {"enabled": settings.security.enabled, "authenticated": auth.authenticated(request)}

    @app.post("/api/auth/login")
    async def login(request: Request):
        check_origin(request)
        if not settings.security.enabled:
            return {"authenticated": True}
        address = request.client.host if request.client else "unknown"
        retry_after = auth.check_rate_limit(address)
        if retry_after:
            return JSONResponse(status_code=429, content={"detail": "Too many attempts; please try again shortly"},
                                headers={"Retry-After": str(retry_after)})
        token = await read_login_token(request)
        if not auth.check_token(token, address):
            return JSONResponse(status_code=401, content={"detail": "Invalid access token"})
        cookie, expires_at = auth.issue_cookie()
        response = JSONResponse({"authenticated": True})
        response.set_cookie(COOKIE_NAME, cookie, max_age=max(1, int(expires_at - time.time())),
                            httponly=True, secure=settings.security.secure_cookie, samesite="lax", path="/")
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        check_origin(request)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE_NAME, path="/", secure=settings.security.secure_cookie,
                               httponly=True, samesite="lax")
        return response

    @app.get("/healthz")
    def health():
        return {"status": "ok", "version": __version__, "mode": "local-read-only",
                "uptime_seconds": round(time.monotonic() - started_at, 1)}

    @app.get("/api/config")
    def config():
        return {"display": settings.display,
                "sources": [{"id": source.id, "label": source.label, "type": source.type,
                             "inventory": {"patterns": list(source.inventory_patterns),
                                           "refresh_seconds": source.inventory_refresh_seconds}}
                            for source in settings.sources.values()]}

    @app.get("/api/files")
    def files():
        result = []
        for file_id, spec in settings.files.items():
            snap = trackers[file_id].snapshot()
            result.append({"id": file_id, "source": spec.source, "label": spec.label,
                           "relative_path": str(spec.path.relative_to(settings.sources[spec.source].root)),
                           "path": str(spec.path), "exists": snap is not None,
                           "size_bytes": snap.size_bytes if snap else 0,
                           "mtime": snap.mtime if snap else None,
                           "generation": snap.generation if snap else None})
        return {"files": result}

    @app.get("/api/announcement")
    def announcement(file_id: str | None = None):
        file_id = file_id or settings.display["default_file"]
        tracker_for(file_id)
        return read_announcement(settings.files[file_id].announcement or settings.announcement)

    @app.get("/api/logs/{file_id}/tail")
    def tail(file_id: str, lines: int = Query(default=settings.display["initial_lines"], ge=1, le=2000)):
        tracker = tracker_for(file_id)
        return indexes[file_id].decorate(tracker.tail(lines))

    @app.get("/api/logs/{file_id}/history")
    def history(file_id: str, before: int = Query(ge=0), generation: str = Query(min_length=1),
                lines: int = Query(default=settings.display["page_lines"], ge=1, le=2000)):
        tracker = tracker_for(file_id)
        return indexes[file_id].decorate(tracker.history(before, lines, generation))

    @app.get("/api/logs/{file_id}/download")
    def download(file_id: str):
        return download_response(tracker_for(file_id))

    @app.get("/api/logs/{file_id}/forward")
    def forward(file_id: str, after: int = Query(ge=0), generation: str = Query(min_length=1),
                lines: int = Query(default=settings.display["page_lines"], ge=1, le=2000)):
        tracker = tracker_for(file_id)
        return indexes[file_id].decorate(tracker.forward(after, lines, generation))

    @app.get("/api/logs/{file_id}/index")
    def index_status(file_id: str):
        tracker_for(file_id)
        return indexes[file_id].status()

    @app.get("/api/logs/{file_id}/window")
    def window(file_id: str, percent: float | None = Query(default=None, ge=0, le=100),
               line: int | None = Query(default=None, ge=1), generation: str | None = None,
               lines: int = Query(default=settings.display["page_lines"], ge=1, le=2000)):
        tracker = tracker_for(file_id)
        item = indexes[file_id]
        if (percent is None) == (line is None):
            raise HTTPException(422, "Specify exactly one percent or line target")
        if line is not None:
            state = item.status()
            if state["generation"] is None:
                raise HTTPException(404, "Configured log does not exist yet")
            if generation is not None and generation != state["generation"]:
                raise HTTPException(409, "Log was replaced or truncated; reload its tail")
            if state["status"] == "error":
                raise HTTPException(503, "The line index is unavailable; retry or use percentage navigation")
            if not state["ready"]:
                return JSONResponse(status_code=202, content={"status": "indexing", "index": state})
            try:
                offset = item.offset_for_line(line, state["generation"])
            except LogError as exc:
                if exc.status_code == 202:
                    return JSONResponse(status_code=202, content={"status": "indexing", "index": item.status()})
                raise
            result = tracker.centered(offset=offset, lines=lines, generation=state["generation"])
            result["target"]["line"] = line
        else:
            result = tracker.centered(percent=percent, lines=lines, generation=generation)
        return item.decorate(result)

    @app.get("/api/logs/{file_id}/stream")
    async def stream(request: Request, file_id: str, cursor: int = Query(ge=0),
                     generation: str = Query(min_length=1),
                     last_event_id: str | None = Header(default=None, alias="Last-Event-ID")):
        tracker = tracker_for(file_id)
        if last_event_id:
            try:
                previous_generation, previous_offset = last_event_id.rsplit(":", 1)
                offset = int(previous_offset)
                if previous_generation == generation and offset >= 0:
                    cursor = offset
            except (ValueError, TypeError):
                pass
        expires_at = auth.verify_cookie(request.cookies.get(COOKIE_NAME)) if settings.security.enabled else None
        if settings.security.enabled and expires_at is None:
            raise HTTPException(401, "Authentication required")
        return EventSourceResponse(stream_events(tracker, cursor, generation, settings.poll_seconds, request, expires_at),
                                   ping=15, headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    @app.get("/api/summary")
    def summary(file_id: str | None = None):
        file_id = file_id or settings.display["default_file"]
        tracker = tracker_for(file_id)
        snap = tracker.snapshot()
        if snap is None:
            return generic_summary(file_id, None)
        if settings.files[file_id].parser == "transfer_status":
            result = tracker.tail(300)
            return parse_transfer_status(result["lines"], file_id, result["mtime"])
        return generic_summary(file_id, snap.mtime)

    @app.get("/api/inventory")
    def source_inventory(source: str, path: str = "", q: str = Query(default="", max_length=256),
                         page: int = Query(default=1, ge=1),
                         page_size: int = Query(default=50, ge=1, le=200)):
        spec = settings.sources.get(source)
        if spec is None:
            raise HTTPException(404, "Unknown configured source ID")
        return inventory.page(spec, page, page_size, path, q)

    @app.get("/api/folders")
    def source_folders(source: str, path: str = "", page: int = Query(default=1, ge=1),
                       page_size: int = Query(default=50, ge=1, le=200)):
        spec = settings.sources.get(source)
        if spec is None:
            raise HTTPException(404, "Unknown configured source ID")
        return inventory.folders(spec, path, page, page_size)

    @app.get("/api/search/files")
    def search_files(q: str = Query(default="", max_length=256),
                     page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200)):
        return search_index.search(q, page, page_size)

    # Wheels carry the UI inside the package; source checkouts keep web/ editable.
    package_root = Path(__file__).resolve().parent
    web_root = package_root / "web"
    if not web_root.is_dir():
        web_root = package_root.parent / "web"

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(web_root / "index.html")

    app.mount("/static", StaticFiles(directory=web_root, check_dir=False), name="static")
    # Exact named assets also work without a URL prefix, keeping deployments simple.
    def fixed_asset(name: str):
        def asset():
            return FileResponse(web_root / name)
        return asset

    for filename in ("app.js", "styles.css", "astrolog-mark.svg", "favicon-16.png",
                     "favicon-32.png", "favicon.ico", "apple-touch-icon.png"):
        app.add_api_route(f"/{filename}", fixed_asset(filename), methods=["GET"], include_in_schema=False)
    return app
