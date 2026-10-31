"""The public logo copies must not drift or expose a shared seed as the mark."""
import base64
from pathlib import Path
import struct
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


class BrandAssetsTests(unittest.TestCase):
    def test_public_copies_match_the_canonical_web_assets(self):
        for name in ['astrolog-mark.svg', 'astrolog-mark-512.png']:
            expected = (ROOT / 'web' / name).read_bytes()
            for folder in ['brand', 'docs']:
                self.assertEqual((ROOT / folder / name).read_bytes(), expected)
        png = (ROOT / 'web/astrolog-mark-512.png').read_bytes()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', png[16:24]), (512, 512))

    def test_shared_ring_is_only_a_mask_input(self):
        svg = ET.parse(ROOT / 'web/astrolog-mark.svg').getroot()
        ns = {'svg':'http://www.w3.org/2000/svg'}
        self.assertEqual(svg.find('svg:title', ns).text, 'AstroLOG')
        image = svg.find('.//svg:mask/svg:image', ns)
        source = (ROOT / 'brand/source/astroslacker-ring-source.png').read_bytes()
        self.assertEqual(image.attrib['href'], 'data:image/png;base64,' + base64.b64encode(source).decode())
        self.assertEqual(len(svg.findall('.//svg:image', ns)), 1)
        self.assertFalse((ROOT / 'brand/astroslacker-ring-source.png').exists())
