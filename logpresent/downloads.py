"""Finite, authenticated-by-router downloads of a configured log file."""

import asyncio
import re
from urllib.parse import quote

from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from .logs import FileTracker

CHUNK_BYTES = 256 * 1024


def download_response(tracker: FileTracker) -> StreamingResponse:
    # Replacing the path cannot redirect this descriptor. A growing log remains finite.
    handle, snapshot = tracker.open_snapshot()
    filename = tracker.path.name
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:180] or "log.txt"
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"

    async def chunks():
        remaining = snapshot.size_bytes
        try:
            while remaining:
                data = await asyncio.to_thread(handle.read, min(CHUNK_BYTES, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data
        finally:
            handle.close()

    # Concurrent truncation may shorten this finite view: do not advertise a
    # Content-Length that would then violate the HTTP response framing.
    return StreamingResponse(chunks(), media_type="application/octet-stream",
                             headers={"Content-Disposition": disposition,
                                      "X-LogPresent-Snapshot-Bytes": str(snapshot.size_bytes),
                                      "Cache-Control": "no-store", "X-Accel-Buffering": "no"},
                             background=BackgroundTask(handle.close))
