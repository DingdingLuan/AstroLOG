"""Bounded navigation and folder confinement against generated local files."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import test_local_viewer as local_fixture


class NavigationTests(unittest.TestCase):
    setUp = local_fixture.LocalViewerTests.setUp
    tail = local_fixture.LocalViewerTests.tail
    write_numbered = local_fixture.LocalViewerTests.write_numbered

    def window(self, **params):
        deadline = time.monotonic() + 3
        while True:
            response = self.client.get("/api/logs/demo.main/window", params=params)
            if response.status_code != 202:
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()
            self.assertEqual(response.json()["status"], "indexing")
            self.assertLess(time.monotonic(), deadline, "Small disposable log index did not become ready")
            time.sleep(0.01)

    def ready_index(self):
        deadline = time.monotonic() + 3
        while True:
            response = self.client.get("/api/logs/demo.main/index")
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            if result["ready"]:
                return result
            self.assertLess(time.monotonic(), deadline, "Small disposable log index did not become ready")
            time.sleep(0.01)

    def test_percent_windows_at_beginning_middle_and_end_are_bounded_complete_utf8(self):
        texts = [f"row {number:05d} – 星系 🌌" for number in range(1000)]
        raw = ("\n".join(texts) + "\n").encode("utf-8")
        self.log.write_bytes(raw)
        windows = [self.window(percent=percent, lines=200) for percent in (0, 50, 100)]
        self.assertEqual(windows[0]["lines"][0]["text"], texts[0])
        self.assertEqual(windows[-1]["lines"][-1]["text"], texts[-1])
        self.assertIn(texts[500], [row["text"] for row in windows[1]["lines"]])
        self.assertTrue(windows[1]["has_more"])
        self.assertTrue(windows[1]["has_newer"])
        for window in windows:
            self.assertGreater(len(window["lines"]), 0)
            self.assertLessEqual(len(window["lines"]), 200)
            rows = window["lines"]
            for row in rows:
                self.assertEqual(raw[row["start"] : row["end"]].decode("utf-8").rstrip("\n"), row["text"])
                self.assertNotIn("\ufffd", row["text"])
            self.assertEqual(window["newer_cursor"], rows[-1]["end"])
            self.assertEqual(window["older_cursor"], rows[0]["start"])

    def test_exact_line_index_updates_after_append_and_restarts_after_rotation(self):
        self.write_numbered(6000)
        middle = self.window(line=4501, lines=200)
        self.assertIn("line 04500", [row["text"] for row in middle["lines"]])
        self.assertLessEqual(len(middle["lines"]), 200)
        one_line = self.window(line=42, lines=1)
        self.assertEqual([row["text"] for row in one_line["lines"]], ["line 00041"])
        self.assertEqual(one_line["start_line"], 42)
        self.assertEqual(one_line["end_line"], 42)
        index = self.ready_index()
        self.assertEqual(index["total_complete_lines"], 6000)
        self.assertEqual(index["indexed_bytes"], self.log.stat().st_size)
        with self.log.open("ab") as output:
            output.write(b"appended complete\npartial")
        appended = self.window(line=6001, lines=20)
        self.assertIn("appended complete", [row["text"] for row in appended["lines"]])
        self.assertNotIn("partial", [row["text"] for row in appended["lines"]])
        updated = self.ready_index()
        self.assertEqual(updated["total_complete_lines"], 6001)
        self.assertEqual(updated["generation"], index["generation"])
        self.log.rename(self.source / "old-run.log")
        self.log.write_text("replacement first\nreplacement second\n", encoding="utf-8")
        replacement = self.window(line=1, lines=10)
        self.assertEqual(replacement["lines"][0]["text"], "replacement first")
        fresh = self.ready_index()
        self.assertEqual(fresh["total_complete_lines"], 2)
        self.assertNotEqual(fresh["generation"], index["generation"])

    def test_forward_and_backward_pages_reconstruct_log_without_duplicates(self):
        self.write_numbered(1201)
        middle = self.window(percent=50, lines=100)
        generation = middle["generation"]
        rows = list(middle["lines"])
        page = middle
        while page["has_more"]:
            response = self.client.get("/api/logs/demo.main/history", params={
                "before": page["older_cursor"], "generation": generation, "lines": 113,
            })
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            rows = page["lines"] + rows
        page = middle
        while page["has_newer"]:
            response = self.client.get("/api/logs/demo.main/forward", params={
                "after": page["newer_cursor"], "generation": generation, "lines": 97,
            })
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            self.assertLessEqual(len(page["lines"]), 97)
            self.assertEqual(rows[-1]["end"], page["lines"][0]["start"])
            rows += page["lines"]
        self.assertEqual([row["text"] for row in rows], [f"line {number:05d}" for number in range(1201)])
        self.assertEqual(len({row["start"] for row in rows}), 1201)

    def test_forward_rejects_old_generation_after_truncation(self):
        self.write_numbered(500)
        middle = self.window(percent=50, lines=50)
        self.log.write_text("new generation\n", encoding="utf-8")
        response = self.client.get("/api/logs/demo.main/forward", params={
            "after": middle["newer_cursor"], "generation": middle["generation"], "lines": 50,
        })
        self.assertEqual(response.status_code, 409, response.text)

    def test_failed_line_index_returns_service_error_instead_of_endless_indexing(self):
        self.write_numbered(500)
        tail = self.tail()
        failed = {"generation": tail["generation"], "status": "error", "ready": False}
        with patch.object(self.client.app.state.indexes["demo.main"], "status", return_value=failed):
            response = self.client.get("/api/logs/demo.main/window", params={"line": 123})
        self.assertEqual(response.status_code, 503, response.text)

    def test_nested_folder_listing_and_inventory_are_scoped_to_selected_folder(self):
        nested = self.source / "nested"
        nested.mkdir()
        (nested / "deeper").mkdir()
        (nested / "snap_059.1.hdf5").write_bytes(b"123")
        (nested / "snap_059.2.hdf5").write_bytes(b"45678")
        (self.source / "snap_059.0.hdf5").write_bytes(b"not selected")
        folders = self.client.get("/api/folders", params={"source": "demo"})
        self.assertEqual(folders.status_code, 200, folders.text)
        self.assertIn("nested", [entry["path"] for entry in folders.json()["folders"]])
        subfolders = self.client.get("/api/folders", params={"source": "demo", "path": "nested"})
        self.assertEqual(subfolders.status_code, 200, subfolders.text)
        self.assertEqual([entry["path"] for entry in subfolders.json()["folders"]], ["nested/deeper"])
        inventory = self.client.get("/api/inventory", params={"source": "demo", "path": "nested"})
        self.assertEqual(inventory.status_code, 200, inventory.text)
        self.assertEqual(inventory.json()["total_count"], 2)
        self.assertEqual(inventory.json()["total_bytes"], 8)
        self.assertEqual({entry["name"] for entry in inventory.json()["files"]}, {"snap_059.1.hdf5", "snap_059.2.hdf5"})

    def test_folder_escape_and_symlink_descent_are_rejected(self):
        outside = self.directory / "outside"
        outside.mkdir()
        (outside / "private.log").write_text("outside private content\n", encoding="utf-8")
        (self.source / "linked").symlink_to(outside, target_is_directory=True)
        paths = ("..", "../outside", str(outside), "nested/../../outside", "nested\\..", "\x00", "linked")
        for endpoint in ("/api/folders", "/api/inventory"):
            for path in paths:
                with self.subTest(endpoint=endpoint, path=path):
                    response = self.client.get(endpoint, params={"source": "demo", "path": path})
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertNotIn("outside private content", response.text)


if __name__ == "__main__":
    unittest.main()
