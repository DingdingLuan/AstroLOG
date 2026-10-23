"""Behavior checks against disposable logs; never touches configured real sources.

Run from the project root with: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import builtins
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import yaml

from logpresent.app import create_app


class LocalViewerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="logpresent-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "source"
        self.source.mkdir()
        self.log = self.source / "run.log"
        self.log.write_text("", encoding="utf-8")
        self.config = self.directory / "config.yaml"
        self.config.write_text(
            yaml.safe_dump(
                {
                    "display": {
                        "title": "Temporary test project",
                        "project": "Disposable logs",
                        "default_file": "demo.main",
                        "initial_lines": 100,
                        "page_lines": 200,
                        "metrics": [],
                    },
                    "data": {
                        "poll_seconds": 0.01,
                        "sources": {
                            "demo": {
                                "type": "local",
                                "root": str(self.source),
                                "files": {
                                    "main": {
                                        "path": "run.log",
                                        "label": "Temporary log",
                                        "parser": "transfer_status",
                                    }
                                },
                                "inventory": {
                                    "patterns": ["snap_059.*"],
                                    "refresh_seconds": 0,
                                },
                            }
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.client = TestClient(create_app(self.config))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def tail(self, **params):
        response = self.client.get("/api/logs/demo.main/tail", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def write_numbered(self, count):
        self.log.write_text(
            "".join(f"line {number:05d}\n" for number in range(count)),
            encoding="utf-8",
        )

    def test_default_tail_returns_last_hundred_complete_lines(self):
        self.write_numbered(350)
        result = self.tail()
        self.assertEqual(len(result["lines"]), 100)
        self.assertEqual(result["lines"][0]["text"], "line 00250")
        self.assertEqual(result["lines"][-1]["text"], "line 00349")
        self.assertTrue(result["has_more"])
        self.assertEqual(result["cursor"], self.log.stat().st_size)
        self.assertEqual(result["older_cursor"], result["lines"][0]["start"])

    def test_history_can_reconstruct_every_line_without_duplicates(self):
        self.write_numbered(551)
        page = self.tail(lines=100)
        generation = page["generation"]
        rows = page["lines"]
        while page["has_more"]:
            response = self.client.get(
                "/api/logs/demo.main/history",
                params={
                    "before": page["older_cursor"],
                    "generation": generation,
                    "lines": 73,
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            self.assertLessEqual(len(page["lines"]), 73)
            if page["lines"] and rows:
                self.assertLess(page["lines"][-1]["start"], rows[0]["start"])
                self.assertEqual(page["lines"][-1]["end"], rows[0]["start"])
            rows = page["lines"] + rows
        self.assertEqual([row["text"] for row in rows], [f"line {n:05d}" for n in range(551)])
        self.assertEqual(len({row["start"] for row in rows}), 551)

    def test_utf8_and_long_lines_preserve_text_and_byte_offsets(self):
        texts = ["开始 🚀", "汉字🌌" * 20000, "finé", "last"]
        raw = "\n".join(texts).encode("utf-8") + b"\n"
        self.log.write_bytes(raw)
        result = self.tail(lines=3)
        self.assertEqual([row["text"] for row in result["lines"]], texts[-3:])
        for row in result["lines"]:
            self.assertEqual(raw[row["start"] : row["end"]].decode("utf-8").rstrip("\n"), row["text"])
        response = self.client.get(
            "/api/logs/demo.main/history",
            params={"before": result["older_cursor"], "generation": result["generation"], "lines": 2},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["text"] for row in response.json()["lines"]], texts[:1])

    def test_empty_log_is_a_valid_tail(self):
        result = self.tail()
        self.assertEqual(result["lines"], [])
        self.assertEqual(result["cursor"], 0)
        self.assertFalse(result["has_more"])

    def test_partial_final_line_does_not_advance_tail_cursor(self):
        self.log.write_bytes(b"first\nunfinished")
        first = self.tail()
        self.assertEqual([row["text"] for row in first["lines"]], ["first"])
        self.assertEqual(first["cursor"], len(b"first\n"))
        with self.log.open("ab") as output:
            output.write(b" completed\n")
        second = self.tail()
        self.assertEqual(second["generation"], first["generation"])
        self.assertEqual([row["text"] for row in second["lines"]], ["first", "unfinished completed"])

    def test_append_keeps_generation_and_preexisting_offsets(self):
        self.write_numbered(4)
        before = self.tail()
        with self.log.open("ab") as output:
            output.write(b"a new row\n")
        after = self.tail()
        self.assertEqual(before["generation"], after["generation"])
        self.assertEqual(after["lines"][:-1], before["lines"])
        self.assertEqual(after["lines"][-1]["start"], before["cursor"])

    def test_rotation_rejects_old_history_generation(self):
        self.write_numbered(120)
        old = self.tail()
        self.log.rename(self.source / "run.log.1")
        self.log.write_text("new run\n", encoding="utf-8")
        current = self.tail()
        self.assertNotEqual(old["generation"], current["generation"])
        self.assertEqual([row["text"] for row in current["lines"]], ["new run"])
        response = self.client.get(
            "/api/logs/demo.main/history",
            params={"before": old["older_cursor"], "generation": old["generation"]},
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_truncation_changes_generation_without_inode_change(self):
        self.write_numbered(120)
        old = self.tail()
        inode = self.log.stat().st_ino
        self.log.write_text("restarted\n", encoding="utf-8")
        self.assertEqual(self.log.stat().st_ino, inode)
        current = self.tail()
        self.assertNotEqual(old["generation"], current["generation"])
        response = self.client.get(
            "/api/logs/demo.main/history",
            params={"before": old["older_cursor"], "generation": old["generation"]},
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_unknown_file_is_not_an_arbitrary_path_reader(self):
        self.log.write_text("allowed\n", encoding="utf-8")
        outside = self.directory / "secret.log"
        outside.write_text("private outside source root\n", encoding="utf-8")
        for endpoint in (
            "/api/logs/demo.secret/tail",
            "/api/logs/demo.%2E%2E%2Fsecret.log/tail",
            "/api/logs/notconfigured/tail",
        ):
            response = self.client.get(endpoint)
            self.assertIn(response.status_code, (400, 403, 404))
            self.assertNotIn("private outside source root", response.text)

    def test_symlink_escape_after_startup_is_rejected(self):
        outside = self.directory / "secret.log"
        outside.write_text("private outside source root\n", encoding="utf-8")
        self.log.unlink()
        self.log.symlink_to(outside)
        response = self.client.get("/api/logs/demo.main/tail")
        self.assertIn(response.status_code, (400, 403, 404))
        self.assertNotIn("private outside source root", response.text)

    def test_transfer_summary_uses_latest_snapshot_record_not_constants(self):
        self.log.write_text(
            "[2026-10-22T15:47:19+08:00] SNAPSHOT 059 state=RUNNING files=1085/1200 "
            "total=1712.92 GiB/1886.61 GiB (90.793%) speed=7.97 MB/s avg=13.94 MB/s "
            "cap=8.00 MB/s ETA=03h43m\n"
            "[2026-10-22T15:47:19+08:00] FILE snap_059.1085.hdf5 1.12 GiB/1.57 GiB "
            "(71.58%) bytes=1203503104/1681446316\n"
            "[2026-10-22T15:48:00+08:00] SNAPSHOT 060 state=RUNNING files=7/20 "
            "total=3.50 GiB/10.00 GiB (35.000%) speed=1.25 MB/s avg=2.00 MB/s "
            "cap=8.00 MB/s ETA=01h02m\n",
            encoding="utf-8",
        )
        response = self.client.get("/api/summary", params={"file_id": "demo.main"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(result["snapshot"], "060")
        self.assertAlmostEqual(result["progress"], 0.35)
        self.assertEqual(result["metrics"]["completed_files"], "7 / 20")
        self.assertEqual(result["metrics"]["transferred"], "3.50 / 10.00 GiB")
        self.assertEqual(result["metrics"]["rate"], "1.25 MB/s")
        self.assertEqual(result["metrics"]["eta"], "01h02m")

    def test_inventory_is_metadata_only_paginated_and_nonrecursive(self):
        for number, size in enumerate((2, 3, 5)):
            (self.source / f"snap_059.{number}.hdf5").write_bytes(b"x" * size)
        (self.source / "unrelated.hdf5").write_bytes(b"not selected")
        nested = self.source / "subdirectory"
        nested.mkdir()
        (nested / "snap_059.999.hdf5").write_bytes(b"not recursive")

        original_builtin_open, original_io_open = builtins.open, io.open

        def reject_snapshot_read(original):
            def checked(file, *args, **kwargs):
                if not isinstance(file, int) and os.fspath(file).endswith(".hdf5"):
                    self.fail(f"Inventory must stat snapshots, never read content: {file}")
                return original(file, *args, **kwargs)
            return checked

        with patch("builtins.open", reject_snapshot_read(original_builtin_open)), patch("io.open", reject_snapshot_read(original_io_open)):
            response = self.client.get("/api/inventory", params={"source": "demo", "page": 1, "page_size": 2})
            second_response = self.client.get("/api/inventory", params={"source": "demo", "page": 2, "page_size": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(second_response.status_code, 200, second_response.text)
        first, second = response.json(), second_response.json()
        self.assertEqual(first["total_count"], 3)
        self.assertEqual(first["total_bytes"], 10)
        self.assertEqual(len(first["files"]), 2)
        self.assertEqual(len(second["files"]), 1)
        names = [entry["name"] for entry in first["files"] + second["files"]]
        self.assertEqual(len(set(names)), 3)
        self.assertTrue(all(name.startswith("snap_059.") for name in names))

    def test_configuration_drives_display_title(self):
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["display"]["title"], "Temporary test project")
        self.assertEqual(result["display"]["default_file"], "demo.main")
        self.assertNotIn(str(self.directory), response.text)


if __name__ == "__main__":
    unittest.main()
