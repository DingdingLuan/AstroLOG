"""A download snapshots a finite byte count without modifying the source."""

from pathlib import Path
import tempfile
import unittest

from logpresent.downloads import download_response
from logpresent.logs import FileTracker


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_growing_file_download_stops_at_original_size_and_leaves_source_untouched(self):
        with tempfile.TemporaryDirectory(prefix="logpresent-download-tests-") as temporary:
            path = Path(temporary) / "日誌.log"
            original = b"a log record\n" * 30000 + b"\x00\xffpartial"
            path.write_bytes(original)
            response = download_response(FileTracker(path))
            growth = b"arrived after download began\n" * 10000
            with path.open("ab") as output:
                output.write(growth)
            expected_mtime = path.stat().st_mtime_ns
            try:
                received = b"".join([chunk async for chunk in response.body_iterator])
            finally:
                await response.body_iterator.aclose()
                await response.background()
            self.assertEqual(received, original)
            self.assertEqual(int(response.headers["x-logpresent-snapshot-bytes"]), len(original))
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])
            self.assertEqual(path.read_bytes(), original + growth)
            self.assertEqual(path.stat().st_mtime_ns, expected_mtime)


if __name__ == "__main__":
    unittest.main()
