"""Filename filtering is scoped, metadata-only, cached, and happens before paging."""

from pathlib import Path
from unittest.mock import patch
import unittest

from fastapi.testclient import TestClient
import yaml

from logpresent.app import create_app
from logpresent.inventory import InventoryCache
import test_local_viewer as local_fixture


class InventorySearchTests(unittest.TestCase):
    setUp = local_fixture.LocalViewerTests.setUp

    def inventory(self, **params):
        response = self.client.get("/api/inventory", params={"source": "demo", **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_filter_across_all_directory_pages_then_paginate_and_total(self):
        for number in range(80):
            (self.source / f"snap_059.part-CAT-{number:03d}.hdf5").write_bytes(b"x" * (number + 1))
        for number in range(10):
            (self.source / f"snap_059.other-{number:03d}.hdf5").write_bytes(b"other")
        (self.source / "unrelated-part-cat.hdf5").write_bytes(b"excluded by configured pattern")
        nested = self.source / "nested"
        nested.mkdir()
        (nested / "snap_059.part-CAT-hidden.hdf5").write_bytes(b"not recursive")
        first = self.inventory(q="  PART-cat  ", page=1, page_size=50)
        second = self.inventory(q="PART-cat", page=2, page_size=50)
        self.assertEqual(first["query"], "PART-cat")
        self.assertEqual(first["directory_total_count"], 90)
        self.assertEqual(first["total_count"], 80)
        self.assertEqual(first["total_bytes"], sum(range(1, 81)))
        self.assertEqual(first["page"], 1)
        self.assertEqual(first["page_size"], 50)
        self.assertEqual(len(first["files"]), 50)
        self.assertEqual(len(second["files"]), 30)
        names = [entry["name"] for entry in first["files"] + second["files"]]
        self.assertEqual(len(set(names)), 80)
        self.assertTrue(all("part-CAT" in name for name in names))

    def test_unicode_casefold_and_literal_filename_substrings(self):
        (self.source / "snap_059.Straße.hdf5").write_bytes(b"abc")
        (self.source / "snap_059.Ä-银河.hdf5").write_bytes(b"galaxy")
        (self.source / "snap_059.literal[1].hdf5").write_bytes(b"literal")
        self.assertEqual(self.inventory(q="STRASSE")["files"][0]["name"], "snap_059.Straße.hdf5")
        self.assertEqual(self.inventory(q="ä-银河")["total_bytes"], 6)
        self.assertEqual(self.inventory(q="[1]")["files"][0]["name"], "snap_059.literal[1].hdf5")
        self.assertEqual(self.inventory(q="*")["total_count"], 0)

    def test_no_matches_and_clearing_query_restore_original_totals(self):
        (self.source / "snap_059.alpha.hdf5").write_bytes(b"alpha")
        (self.source / "snap_059.beta.hdf5").write_bytes(b"beta")
        no_matches = self.inventory(q="missing")
        self.assertEqual(no_matches["files"], [])
        self.assertEqual(no_matches["total_count"], 0)
        self.assertEqual(no_matches["total_bytes"], 0)
        self.assertEqual(no_matches["directory_total_count"], 2)
        cleared = self.inventory(q=" \t ")
        self.assertEqual(cleared["query"], "")
        self.assertEqual(cleared["total_count"], 2)
        self.assertEqual(cleared["total_bytes"], 9)
        self.assertEqual(self.inventory()["total_count"], 2)

    def test_query_length_and_pagination_bounds_remain_enforced(self):
        self.assertEqual(self.inventory(q="x" * 256)["total_count"], 0)
        for params in ({"q": "x" * 257}, {"q": "x", "page": 0}, {"q": "x", "page_size": 201}):
            response = self.client.get("/api/inventory", params={"source": "demo", **params})
            self.assertEqual(response.status_code, 422, response.text)

    def test_queries_reuse_unfiltered_metadata_cache_without_reading_files(self):
        (self.source / "snap_059.alpha.hdf5").write_bytes(b"alpha")
        (self.source / "snap_059.beta.hdf5").write_bytes(b"beta")
        source = self.client.app.state.settings.sources["demo"]
        # The endpoint fixture uses refresh=0; retain its source identity and give
        # this isolated cache a positive TTL for the cache reuse behavior check.
        from dataclasses import replace
        source = replace(source, inventory_refresh_seconds=3600)
        cache = InventoryCache()
        with patch.object(cache, "_scan", wraps=cache._scan) as scan, patch.object(
                Path, "open", side_effect=AssertionError("Filename search must never read file content")):
            full = cache.page(source)
            alpha = cache.page(source, q="ALPHA")
            beta = cache.page(source, q="beta")
            cleared = cache.page(source, q="")
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(full["total_count"], 2)
        self.assertEqual(alpha["total_count"], 1)
        self.assertEqual(beta["total_count"], 1)
        self.assertEqual(cleared["total_count"], 2)
        self.assertEqual(full["updated_at"], alpha["updated_at"])

    def test_filtering_remains_protected_by_cookie_authentication(self):
        (self.source / "snap_059.private-item.hdf5").write_bytes(b"private")
        token = self.directory / "token"
        token.write_text("temporary-inventory-search-token\n", encoding="utf-8")
        token.chmod(0o600)
        config = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        config["security"] = {"enabled": True, "token_file": str(token)}
        self.config.write_text(yaml.safe_dump(config), encoding="utf-8")
        with TestClient(create_app(self.config)) as client:
            locked = client.get("/api/inventory", params={"source": "demo", "q": "private-item"})
            self.assertEqual(locked.status_code, 401)
            self.assertNotIn("snap_059.private-item", locked.text)
            self.assertEqual(client.post("/api/auth/login", json={"token": "temporary-inventory-search-token"}).status_code, 200)
            allowed = client.get("/api/inventory", params={"source": "demo", "q": "PRIVATE-ITEM"})
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(allowed.json()["total_count"], 1)


if __name__ == "__main__":
    unittest.main()
