#!/usr/bin/env python3
"""Sync public brand assets and build the browser-only demo (stdlib only)."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    destination = ROOT / "docs"
    destination.mkdir(exist_ok=True)
    brand = ROOT / "brand"
    brand.mkdir(exist_ok=True)
    for name in ("astrolog-mark.svg", "astrolog-mark-512.png"):
        shutil.copy2(ROOT / "web" / name, brand / name)
    for source in (ROOT / "web").iterdir():
        if source.is_file() and source.name != "theme.js" and source.suffix in {".html", ".css", ".js", ".svg", ".png", ".ico"}:
            shutil.copy2(source, destination / source.name)
    (destination / "static").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "web/theme.js", destination / "static/theme.js")
    shutil.copy2(ROOT / "demo" / "demo-adapter.js", destination / "demo-adapter.js")
    index = destination / "index.html"
    content = index.read_text(encoding="utf-8")
    anchor = '  <script src="./app.js" defer></script>'
    if content.count(anchor) != 1:
        raise SystemExit("Expected one app.js script tag; update the demo build adapter injection.")
    content = content.replace(anchor, '  <meta name="description" content="Try AstroLOG: a live log viewer. This interactive demo uses synthetic data entirely in your browser.">\n  <script src="./demo-adapter.js" defer></script>\n' + anchor)
    content = content.replace("<title>AstroLOG</title>", "<title>AstroLOG · Interactive demo</title>")
    index.write_text(content, encoding="utf-8")
    (destination / ".nojekyll").touch()
    print("Built docs/: real AstroLOG UI + browser-only synthetic demo adapter.")


if __name__ == "__main__":
    main()
