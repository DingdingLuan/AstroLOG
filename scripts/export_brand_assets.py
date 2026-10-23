"""Export the portable AstroLOG SVG to browser PNGs and a multi-size ICO.

Requires only the optional Playwright development environment and Chromium.
Run from any directory; outputs live beside web/astrolog-mark.svg.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
import struct

from playwright.async_api import async_playwright


PROJECT = Path(__file__).resolve().parents[1]
WEB = PROJECT / "web"


def ico_bitmap(size: int, rgba: bytes) -> bytes:
    """A standard 32-bit DIB with a bottom-up transparency mask."""
    stride = ((size + 31) // 32) * 4
    pixels = bytearray()
    mask = bytearray()
    for row in range(size - 1, -1, -1):
        row_mask = bytearray(stride)
        for column in range(size):
            offset = (row * size + column) * 4
            red, green, blue, alpha = rgba[offset : offset + 4]
            pixels.extend((blue, green, red, alpha))
            if alpha == 0:
                row_mask[column // 8] |= 1 << (7 - column % 8)
        mask.extend(row_mask)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         len(pixels) + len(mask), 0, 0, 0, 0)
    return header + pixels + mask


async def main() -> None:
    svg = base64.b64encode((WEB / "astrolog-mark.svg").read_bytes()).decode("ascii")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("<!doctype html><title>AstroLOG asset export</title>")
            frames = {}
            for size in (16, 32, 48, 180, 512):
                result = await page.evaluate("""async ({source, size}) => {
                    const image = new Image();
                    image.src = `data:image/svg+xml;base64,${source}`;
                    await image.decode();
                    const canvas = document.createElement('canvas');
                    canvas.width = canvas.height = size;
                    const context = canvas.getContext('2d');
                    if (size === 180) {
                        context.fillStyle = '#f5f7f9';
                        context.fillRect(0, 0, size, size);
                        const inset = 14;
                        context.drawImage(image, inset, inset, size - inset * 2, size - inset * 2);
                    } else {
                        context.drawImage(image, 0, 0, size, size);
                    }
                    const pixels = context.getImageData(0, 0, size, size).data;
                    let binary = '';
                    for (const byte of pixels) binary += String.fromCharCode(byte);
                    return {png: canvas.toDataURL('image/png').split(',')[1], rgba: btoa(binary)};
                }""", {"source": svg, "size": size})
                name = {16: "favicon-16.png", 32: "favicon-32.png",
                        180: "apple-touch-icon.png", 512: "astrolog-mark-512.png"}.get(size)
                if name:
                    (WEB / name).write_bytes(base64.b64decode(result["png"]))
                if size <= 48:
                    frames[size] = ico_bitmap(size, base64.b64decode(result["rgba"]))
            directory = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
            offset = 6 + 16 * len(frames)
            for size, bitmap in frames.items():
                directory.extend(struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32,
                                             len(bitmap), offset))
                offset += len(bitmap)
            (WEB / "favicon.ico").write_bytes(directory + b"".join(frames.values()))
            print("Exported: favicon-16.png, favicon-32.png, favicon.ico (16/32/48), "
                  "apple-touch-icon.png, astrolog-mark-512.png")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
