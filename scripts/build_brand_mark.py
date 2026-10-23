"""Compose AstroLOG's center with the unmodified AstroSlacker-family ring alpha.

The source PNG is preserved byte for byte. Its existing outer silhouette and
grain supply the SVG alpha mask; only the central product symbol is replaced.
No tracing, smoothing, particle synthesis or blur is applied to the ring.
"""

from __future__ import annotations

import base64
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
COLOR = "#f48120"


def main() -> None:
    source = PROJECT / "brand/astroslacker-ring-source.png"
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 1024 1024" fill="none">
  <title>AstroLOG</title>
  <desc>The original AstroSlacker-family outer ring and grain, with an AstroLOG streaming log prompt at its center.</desc>
  <defs>
    <mask id="astroslacker-ring-alpha" maskUnits="userSpaceOnUse" x="0" y="0" width="1024" height="1024" style="mask-type:alpha">
      <image x="0" y="0" width="1024" height="1024" href="data:image/png;base64,{encoded}"/>
    </mask>
  </defs>
  <path fill="{COLOR}" fill-rule="evenodd" mask="url(#astroslacker-ring-alpha)" d="M0 0h1024v1024H0ZM736.5 511.5a225 225 0 1 0-450 0 225 225 0 1 0 450 0Z"/>
  <g transform="translate(-.5 -.5) scale(8)" stroke="{COLOR}" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round">
    <path d="m47 53 11 11-11 11"/>
    <path d="M67 53h14M67 64h10M67 75h14"/>
  </g>
</svg>
'''
    (PROJECT / "web/astrolog-mark.svg").write_text(svg, encoding="utf-8")
    print("Built AstroLOG SVG with the original outer-ring alpha; center replaced only.")


if __name__ == "__main__":
    main()
