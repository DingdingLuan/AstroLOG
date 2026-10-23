#!/usr/bin/env python3
"""Create a private viewer passphrase, without shell arguments or printing it."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import secrets
import tempfile


def main():
    default = Path(__file__).resolve().parents[1] / "var" / "auth" / "access-token"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=default, help="Private token file configured in YAML")
    parser.add_argument("--generate", action="store_true", help="Generate a random passphrase instead of prompting")
    parser.add_argument("--force", action="store_true", help="Replace an existing token when generating")
    args = parser.parse_args()
    target = args.file.expanduser().resolve()
    if args.generate and target.exists() and not args.force:
        parser.error("Token already exists. Use --force to rotate it, or run without --generate to choose one.")
    if args.generate:
        token = secrets.token_urlsafe(24)
    else:
        token = getpass.getpass("New access passphrase (at least 12 characters): ")
        if token != getpass.getpass("Repeat passphrase: "):
            parser.error("Passphrases do not match; existing token is unchanged.")
    if len(token) < 12 or len(token.encode("utf-8")) > 1024 or "\n" in token or "\r" in token or token != token.strip():
        parser.error("Use 12 or more characters, at most 1024 UTF-8 bytes, without newlines or edge whitespace.")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, prefix=".access-token-", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Saved private access passphrase to {target}")
    print("Restart AstroLOG to apply it. Existing browser sessions will then require the new passphrase.")


if __name__ == "__main__":
    main()
