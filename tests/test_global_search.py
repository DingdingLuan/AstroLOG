"""Bounded background global filename search over disposable configured roots."""

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import yaml

from logpresent.app import create_app
from logpresent.inventory import _open_directory
from logpresent.logs import LogError
from logpresent.search import GlobalFileIndex


class GlobalSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astrolog-global-search-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.alpha = self.root / "alpha"
        self.beta = self.root / "beta"
        self.alpha.mkdir()
        self.beta.mkdir()
        (self.alpha / "run.log").write_bytes(b"alpha\n")
        (self.beta / "run.log").write_bytes(b"beta\n")
        self.config_path = self.root / "viewer.yaml"
        self.config = {"display": {"default_file": "alpha.main"}, "data": {"sources": {
            "alpha": {"type": "local", "root": str(self.alpha), "label": "Alpha source",
                      "files": {"main": {"path": "run.log"}}, "inventory": {"patterns": ["snap_*"]}},
            "beta": {"type": "local", "root": str(self.beta), "label": "Beta source",
                     "files": {"main": {"path": "run.log"}}, "inventory": {"patterns": ["snap_*"]}},
        }}}
        self.client = self.new_client()

    def new_client(self):
        self.config_path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        client = TestClient(create_app(self.config_path))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def ready(self, q="", client=None, **params):
        client = client or self.client
        deadline = time.monotonic() + 3
        while True:
            response = client.get("/api/search/files", params={"q": q, **params})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            if result["status"] != "indexing":
                return result
            self.assertLess(time.monotonic(), deadline, "Small global index did not finish")
            time.sleep(0.01)

    def ready_index(self, index):
        deadline = time.monotonic() + 3
        while True:
            result = index.search()
            if result["status"] != "indexing":
                return result
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    def test_cross_source_recursive_search_ignores_inventory_patterns_and_excludes_symlinks(self):
        nested = self.alpha / "nested" / "deeper"
        nested.mkdir(parents=True)
        (nested / "unregistered.LOG").write_bytes(b"deep log\n")
        outside = self.root / "private-outside"
        outside.mkdir()
        (outside / "private-secret.log").write_bytes(b"outside")
        (self.alpha / "linked").symlink_to(outside, target_is_directory=True)
        (self.alpha / "linked-secret.log").symlink_to(outside / "private-secret.log")
        result = self.ready("  .lOg  ")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["query"], ".lOg")
        self.assertEqual(result["total_count"], 3)
        self.assertEqual({row["source"] for row in result["files"]}, {"alpha", "beta"})
        rows = {(row["source"], row["path"]): row for row in result["files"]}
        self.assertEqual(rows[("alpha", "run.log")]["log_id"], "alpha.main")
        self.assertEqual(rows[("beta", "run.log")]["log_id"], "beta.main")
        self.assertIsNone(rows[("alpha", "nested/deeper/unregistered.LOG")]["log_id"])
        self.assertEqual(rows[("alpha", "run.log")]["source_label"], "Alpha source")
        self.assertGreaterEqual(result["skipped"]["symlinks"], 2)
        self.assertFalse(result["truncated"])
        self.assertTrue(all(not row["is_dir"] and not row["path"].startswith("/") for row in result["files"]))
        self.assertEqual(self.ready("secret")["total_count"], 0)
        self.assertEqual(self.client.get("/api/inventory", params={"source": "alpha", "q": "log"}).json()["total_count"], 0)

    def test_initial_indexing_exposes_registered_seed_and_explicit_nonfinal_status(self):
        (self.alpha / "unregistered.log").write_bytes(b"extra")
        index = self.client.app.state.search_index
        release = threading.Event()
        original = index._scan

        def held_scan(rows, skipped):
            release.wait(2)
            original(rows, skipped)

        with patch.object(index, "_scan", side_effect=held_scan):
            try:
                result = self.client.get("/api/search/files", params={"q": "run.log"}).json()
                self.assertEqual(result["status"], "indexing")
                self.assertEqual(result["indexed_files"], 2)
                self.assertEqual(result["total_count"], 2)
                self.assertEqual({row["log_id"] for row in result["files"]}, {"alpha.main", "beta.main"})
            finally:
                release.set()
            final = self.ready("log")
        self.assertEqual(final["status"], "ready")
        self.assertEqual(final["total_count"], 3)

    def test_global_filtering_precedes_pagination_and_supports_unicode_casefold(self):
        nested = self.alpha / "outputs"
        nested.mkdir()
        for number in range(75):
            destination = nested if number < 40 else self.beta
            (destination / f"batch-银河-{number:03d}.log").write_bytes(b"x" * (number + 1))
        (self.alpha / "Straße.LOG").write_bytes(b"casefold")
        first = self.ready("  银河  ", page=1, page_size=50)
        second = self.ready("银河", page=2, page_size=50)
        self.assertEqual(first["total_count"], 75)
        self.assertEqual(first["total_bytes"], sum(range(1, 76)))
        self.assertEqual(len(first["files"]), 50)
        self.assertEqual(len(second["files"]), 25)
        self.assertEqual(len({(row["source"], row["path"]) for row in first["files"] + second["files"]}), 75)
        self.assertEqual(self.ready("STRASSE")["files"][0]["name"], "Straße.LOG")
        self.assertEqual(self.ready("no-such-file")["total_count"], 0)

    def test_queries_reuse_one_index_and_ttl_refresh_discovers_new_files(self):
        index = self.client.app.state.search_index
        with patch.object(index, "_scan", wraps=index._scan) as scan:
            self.ready("run")
            self.ready(".LOG")
            self.ready("nothing")
            self.assertEqual(scan.call_count, 1)
            (self.alpha / "fresh.log").write_bytes(b"new")
            self.assertEqual(self.ready("fresh")["total_count"], 0)
            with index._lock:
                index._finished_at = time.monotonic() - 60
            self.assertEqual(self.ready("fresh")["total_count"], 1)
            self.assertEqual(scan.call_count, 2)

    def test_ttl_refresh_preserves_existing_second_page_until_fresh_index_finishes(self):
        for number in range(75):
            (self.alpha / f"batch-{number:03d}.log").write_bytes(b"fixture")
        original_page = self.ready("batch", page=2, page_size=50)
        self.assertEqual(original_page["total_count"], 75)
        self.assertEqual(len(original_page["files"]), 25)
        (self.alpha / "batch-new.log").write_bytes(b"newly created")
        index = self.client.app.state.search_index
        release = threading.Event()
        original_scan = index._scan

        def held_scan(rows, skipped):
            release.wait(2)
            original_scan(rows, skipped)

        with patch.object(index, "_scan", side_effect=held_scan):
            try:
                with index._lock:
                    index._finished_at = time.monotonic() - 60
                refreshing = self.client.get("/api/search/files", params={"q": "batch", "page": 2, "page_size": 50}).json()
                self.assertEqual(refreshing["status"], "indexing")
                self.assertEqual(refreshing["total_count"], 75)
                self.assertEqual(refreshing["files"], original_page["files"])
                self.assertEqual(refreshing["updated_at"], original_page["updated_at"])
            finally:
                release.set()
            final_page = self.ready("batch", page=2, page_size=50)
        self.assertEqual(final_page["total_count"], 76)
        self.assertEqual(len(final_page["files"]), 26)
        self.assertIn("batch-new.log", [row["name"] for row in final_page["files"]])

    def test_file_folder_depth_and_entry_limits_report_partial_results_explicitly(self):
        nested = self.alpha / "deep"
        nested.mkdir()
        (nested / "hidden-by-depth.log").write_bytes(b"deep")
        for number in range(8):
            (self.alpha / f"extra-{number}.log").write_bytes(b"sample")
        app = self.client.app
        for options, reason in (({"max_files": 3}, "file_limit"),
                                ({"max_depth": 0}, "depth_limit"),
                                ({"max_folders": 2}, "folder_limit"),
                                ({"max_entries": 1}, "entry_limit")):
            with self.subTest(options=options):
                index = GlobalFileIndex(app.state.settings.sources, app.state.settings.files,
                                        app.state.trackers, **options)
                try:
                    result = self.ready_index(index)
                    self.assertEqual(result["status"], "partial")
                    self.assertTrue(result["truncated"])
                    self.assertGreater(result["skipped"][reason], 0)
                    if reason == "file_limit":
                        self.assertLessEqual(result["indexed_files"], 3)
                finally:
                    index.close()

    def test_unreadable_source_marks_partial_without_leaking_error_paths(self):
        def protected(source, relative):
            if source.id == "alpha":
                raise LogError(403, "private path should not be returned")
            return _open_directory(source, relative)

        with patch("logpresent.search._open_directory", side_effect=protected):
            result = self.ready("run")
        self.assertEqual(result["status"], "partial")
        self.assertGreater(result["skipped"]["unreadable"], 0)
        self.assertNotIn("private path", str(result))
        self.assertNotIn(str(self.root), str(result))

    def test_indexing_uses_metadata_only_and_preserves_source_content_and_mtime(self):
        source = self.alpha / "run.log"
        before, mtime = source.read_bytes(), source.stat().st_mtime_ns
        with patch.object(Path, "open", side_effect=AssertionError("Global search must not read file content")):
            result = self.ready("run")
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(source.stat().st_mtime_ns, mtime)

    def test_global_search_bounds_and_existing_cookie_authentication(self):
        self.assertEqual(self.ready("x" * 256)["total_count"], 0)
        for params in ({"q": "x" * 257}, {"q": "run", "page": 0}, {"q": "run", "page_size": 201}):
            response = self.client.get("/api/search/files", params=params)
            self.assertEqual(response.status_code, 422)
        token = self.root / "private-token"
        token.write_text("temporary-global-search-token\n", encoding="utf-8")
        token.chmod(0o600)
        self.config["security"] = {"enabled": True, "token_file": str(token)}
        client = self.new_client()
        locked = client.get("/api/search/files", params={"q": "run"})
        self.assertEqual(locked.status_code, 401)
        self.assertNotIn("run.log", locked.text)
        self.assertIsNone(client.app.state.search_index._future)
        self.assertEqual(client.post("/api/auth/login", json={"token": "temporary-global-search-token"}).status_code, 200)
        self.assertEqual(self.ready("run.log", client)["total_count"], 2)


if __name__ == "__main__":
    unittest.main()
