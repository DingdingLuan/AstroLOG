"""Remote inventories contain metadata only, not snapshot replicas."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import yaml

from logpresent.app import create_app
from logpresent.config import load_config
from logpresent.inventory import InventoryCache
from logpresent.logs import LogError
from logpresent.manifest import ManifestStore


class ManifestInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astrolog-manifest-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache"
        (self.cache / "logs").mkdir(parents=True)
        (self.cache / "logs" / "run.log").write_text("cached real log\n")
        self.manifest = self.root / "inventory.json"
        self.config_path = self.root / "viewer.yaml"
        self.config = {"data": {"sources": {"remote": {
            "root": str(self.cache), "files": {"main": "logs/run.log"},
            "inventory": {"patterns": ["*"], "manifest": "inventory.json", "refresh_seconds": 0},
        }}}}
        self.config_path.write_text(yaml.safe_dump(self.config))
        self.document = {
            "version": 1, "updated_at": datetime.now(timezone.utc).isoformat(),
            "truncated": False, "skipped": 0,
            "entries": [
                {"path": "output", "size_bytes": 10**12, "mtime": 1234, "is_dir": True, "size_known": True},
                {"path": "output/snapdir_001", "size_bytes": 10**12, "mtime": 1234,
                 "is_dir": True, "size_known": True},
                {"path": "output/snapdir_001/snap_001.0.hdf5", "size_bytes": 10**12,
                 "mtime": 1234, "is_dir": False},
                {"path": "logs/run.log", "size_bytes": 999, "mtime": 1234, "is_dir": False},
            ],
        }
        self.write_manifest()

    def write_manifest(self):
        replacement = self.root / "next-inventory.json"
        replacement.write_text(json.dumps(self.document))
        replacement.replace(self.manifest)

    def new_client(self):
        client = TestClient(create_app(self.config_path))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_config_is_optional_and_manifest_path_resolves_from_config_directory(self):
        configured = load_config(self.config_path).sources["remote"]
        self.assertEqual(configured.inventory_manifest, self.manifest)
        for bad in ("", "   ", [], "bad\npath", "bad\x00path"):
            with self.subTest(value=bad):
                self.config["data"]["sources"]["remote"]["inventory"]["manifest"] = bad
                self.config_path.write_text(yaml.safe_dump(self.config))
                with self.assertRaisesRegex(ValueError, "inventory.manifest"):
                    load_config(self.config_path)

    def test_browse_folders_and_sizes_without_snapshot_content_or_local_directory(self):
        client = self.new_client()
        source = client.app.state.settings.sources["remote"]
        # There is no output directory or snapshot payload anywhere in the cache.
        self.assertFalse((self.cache / "output").exists())
        cache = InventoryCache()
        with patch("logpresent.inventory._open_directory", side_effect=AssertionError("Do not scan replica paths")):
            folders = cache.folders(source)
            self.assertEqual({row["name"] for row in folders["folders"]}, {"logs", "output"})
            page = cache.page(source, path="output/snapdir_001", q="HDF5", page_size=1)
        self.assertEqual(page["total_count"], 1)
        self.assertEqual(page["total_bytes"], 10**12)
        self.assertEqual(page["origin"], "manifest")
        self.assertFalse(page["stale"])
        self.assertEqual(page["updated_at"], self.document["updated_at"])
        directory = cache.page(source, path="output")["files"][0]
        self.assertTrue(directory["size_known"])
        self.assertEqual(directory["size_bytes"], 10**12)
        for path in ("../private", "/tmp", "logs/../output", "bad\\path"):
            with self.subTest(path=path):
                with self.assertRaises(LogError):
                    cache.page(source, path=path)
        self.assertEqual(client.get("/api/logs/remote.snapshot/download").status_code, 404)

    def test_global_search_combines_remote_metadata_and_actual_registered_logs(self):
        client = self.new_client()
        index = client.app.state.search_index
        deadline = time.monotonic() + 3
        while True:
            result = index.search()
            if result["status"] != "indexing":
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(result["status"], "ready")
        rows = {row["path"]: row for row in result["files"]}
        self.assertEqual(rows["output/snapdir_001/snap_001.0.hdf5"]["size_bytes"], 10**12)
        self.assertIsNone(rows["output/snapdir_001/snap_001.0.hdf5"]["log_id"])
        self.assertEqual(rows["logs/run.log"]["size_bytes"], len(b"cached real log\n"))
        self.assertEqual(rows["logs/run.log"]["log_id"], "remote.main")
        self.assertNotIn("inventory.json", rows)

    def test_failed_refresh_retains_last_good_snapshot_and_marks_stale(self):
        store = ManifestStore()
        original = store.get(self.manifest)
        self.manifest.write_text("{incomplete json")
        retained = store.get(self.manifest)
        self.assertTrue(retained.stale)
        self.assertEqual(retained.entries, original.entries)
        self.manifest.unlink()
        self.assertTrue(store.get(self.manifest).stale)
        with self.assertRaises(LogError) as raised:
            ManifestStore().get(self.manifest)
        self.assertEqual(raised.exception.status_code, 503)
        self.write_manifest()
        self.assertFalse(store.get(self.manifest).stale)
        self.document["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.write_manifest()
        self.assertTrue(store.get(self.manifest).stale)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_fifo_inventory_is_rejected_without_waiting_for_a_writer(self):
        fifo = self.root / "inventory.fifo"
        os.mkfifo(fifo)
        finished = threading.Event()
        failures = []

        def read():
            try:
                ManifestStore().get(fifo)
            except LogError as exc:
                failures.append(exc.status_code)
            finally:
                finished.set()

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        self.assertTrue(finished.wait(1), "A nonregular manifest must not block waiting for a writer")
        worker.join(timeout=1)
        self.assertEqual(failures, [503])

    def test_invalid_entries_and_limits_reject_whole_snapshot(self):
        base = self.document["entries"][0].copy()
        for update in ({"path": "../secret"}, {"path": "/secret"}, {"path": "bad\\path"},
                       {"path": "output//nested"}, {"name": "different"}, {"size_bytes": -1},
                       {"size_bytes": True}, {"mtime": float("nan")}, {"is_dir": "yes"},
                       {"size_known": "yes"}):
            with self.subTest(update=update):
                self.document["entries"] = [{**base, **update}]
                self.write_manifest()
                with self.assertRaises(LogError):
                    ManifestStore().get(self.manifest)
        self.document["entries"] = [base, base]
        self.write_manifest()
        with self.assertRaises(LogError):
            ManifestStore().get(self.manifest)
        self.document["entries"] = [base]
        self.write_manifest()
        with patch("logpresent.manifest.MAX_MANIFEST_ENTRIES", 0):
            with self.assertRaises(LogError):
                ManifestStore().get(self.manifest)
        with patch("logpresent.manifest.MAX_MANIFEST_BYTES", 1):
            with self.assertRaises(LogError):
                ManifestStore().get(self.manifest)

    def test_partial_manifest_and_read_failure_are_visible_to_search_and_inventory(self):
        self.document["truncated"] = True
        self.document["skipped"] = 2
        self.write_manifest()
        client = self.new_client()
        page = client.get("/api/inventory", params={"source": "remote"}).json()
        self.assertTrue(page["truncated"])
        self.assertEqual(page["skipped"], 2)
        index = client.app.state.search_index
        deadline = time.monotonic() + 3
        while True:
            result = index.search()
            if result["status"] != "indexing":
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["skipped"]["unreadable"], 2)


if __name__ == "__main__":
    unittest.main()
