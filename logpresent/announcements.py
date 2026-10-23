"""Read only configured Markdown files, dynamically and with strict size bounds."""

import hashlib
import os
import stat

from .config import Announcement

MAX_MARKDOWN_BYTES = 64 * 1024


def read_announcement(spec: Announcement | None) -> dict:
    result = {"enabled": False, "title": spec.title if spec else "About this workspace",
              "markdown": "", "revision": None,
              "refresh_seconds": spec.refresh_seconds if spec else 15}
    if spec is None:
        return result
    try:
        # The configured canonical file may be atomically replaced, but no request
        # can supply a path or redirect this reader using a replacement symlink.
        if spec.path.resolve() != spec.path:
            return result
        descriptor = os.open(spec.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_MARKDOWN_BYTES:
                return result
            content = source.read(MAX_MARKDOWN_BYTES + 1)
        if len(content) > MAX_MARKDOWN_BYTES:
            return result
        markdown = content.decode("utf-8")
    except (OSError, UnicodeError, RuntimeError, ValueError):
        return result
    result.update(enabled=True, markdown=markdown, revision=hashlib.sha256(content).hexdigest())
    return result
