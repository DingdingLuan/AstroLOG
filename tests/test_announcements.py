"""Dynamic configured Markdown announcements, using only disposable files."""

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
import yaml

from logpresent.announcements import MAX_MARKDOWN_BYTES
from logpresent.app import create_app


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astrolog-announcement-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "config").mkdir()
        (self.root / "content").mkdir()
        (self.root / "logs").mkdir()
        (self.root / "logs" / "main.log").write_text("example log\n", encoding="utf-8")
        self.markdown = self.root / "content" / "private-announcement.md"
        self.config_path = self.root / "config" / "viewer.yaml"
        self.config = {
            "display": {"default_file": "demo.main", "announcement": {
                "path": "../content/private-announcement.md", "title": "About this workspace",
                "refresh_seconds": 1}},
            "data": {"sources": {"demo": {"type": "local", "root": "../logs", "files": {
                "main": {"path": "main.log"}, "other": {"path": "main.log"}}}}},
        }

    def client(self):
        self.config_path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        client = TestClient(create_app(self.config_path))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def announcement(self, client, file_id=None):
        params = {"file_id": file_id} if file_id else {}
        response = client.get("/api/announcement", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(set(result), {"enabled", "title", "markdown", "revision", "refresh_seconds"})
        self.assertNotIn(str(self.markdown), response.text)
        self.assertNotIn("private-announcement.md", response.text)
        return result

    def test_create_edit_atomic_replace_and_delete_without_restart(self):
        client = self.client()
        missing = self.announcement(client)
        self.assertFalse(missing["enabled"])
        self.assertEqual(missing["markdown"], "")
        self.assertEqual(missing["refresh_seconds"], 1)
        self.markdown.write_text("# First version\n\nScientific notes 🌌\n", encoding="utf-8")
        first = self.announcement(client)
        self.assertTrue(first["enabled"])
        self.assertIn("Scientific notes 🌌", first["markdown"])
        original_bytes = self.markdown.read_bytes()
        original_mtime = self.markdown.stat().st_mtime_ns
        unchanged = self.announcement(client)
        self.assertEqual(first["revision"], unchanged["revision"])
        self.assertEqual(self.markdown.read_bytes(), original_bytes)
        self.assertEqual(self.markdown.stat().st_mtime_ns, original_mtime)
        replacement = self.markdown.with_suffix(".new")
        replacement.write_text("# Second version\n\nUpdated instructions.\n", encoding="utf-8")
        replacement.replace(self.markdown)
        second = self.announcement(client)
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertIn("Updated instructions.", second["markdown"])
        self.markdown.unlink()
        deleted = self.announcement(client)
        self.assertFalse(deleted["enabled"])
        self.assertEqual(deleted["markdown"], "")
        self.assertIsNone(deleted["revision"])

    def test_size_limit_and_invalid_utf8_hide_content_without_error_details(self):
        client = self.client()
        self.markdown.write_bytes(b"x" * MAX_MARKDOWN_BYTES)
        self.assertTrue(self.announcement(client)["enabled"])
        self.markdown.write_bytes(b"oversized" + b"x" * MAX_MARKDOWN_BYTES)
        oversized = self.announcement(client)
        self.assertFalse(oversized["enabled"])
        self.assertEqual(oversized["markdown"], "")
        self.markdown.write_bytes(b"invalid utf8 \xff\xfe")
        invalid = self.announcement(client)
        self.assertFalse(invalid["enabled"])
        self.assertIsNone(invalid["revision"])

    def test_unconfigured_announcements_are_disabled_and_unknown_file_is_rejected(self):
        self.config["display"].pop("announcement")
        client = self.client()
        result = self.announcement(client)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["markdown"], "")
        response = client.get("/api/announcement", params={"file_id": "not.configured"})
        self.assertEqual(response.status_code, 404)

    def test_per_file_override_and_missing_override_does_not_fall_back(self):
        self.markdown.write_text("Global announcement", encoding="utf-8")
        override = self.root / "content" / "other.md"
        override.write_text("File-specific announcement", encoding="utf-8")
        self.config["data"]["sources"]["demo"]["files"]["other"]["announcement"] = {
            "path": "../content/other.md", "title": "About this log", "refresh_seconds": 2}
        client = self.client()
        self.assertEqual(self.announcement(client, "demo.main")["markdown"], "Global announcement")
        other = self.announcement(client, "demo.other")
        self.assertEqual(other["markdown"], "File-specific announcement")
        self.assertEqual(other["title"], "About this log")
        self.assertEqual(other["refresh_seconds"], 2)
        override.unlink()
        self.assertFalse(self.announcement(client, "demo.other")["enabled"])
        self.assertTrue(self.announcement(client, "demo.main")["enabled"])

    def test_api_returns_markdown_as_plain_json_text_and_hides_configured_paths(self):
        raw = '# Example\n<script>alert("synthetic")</script>\n<img src=x onerror=alert(1)>\n'
        self.markdown.write_text(raw, encoding="utf-8")
        client = self.client()
        result = self.announcement(client)
        self.assertEqual(result["markdown"], raw)
        response = client.get("/api/config")
        self.assertNotIn("announcement", response.json()["display"])
        self.assertNotIn("private-announcement.md", response.text)
        self.assertNotIn(str(self.root), response.text)
        self.assertIn("application/json", client.get("/api/announcement").headers["content-type"])

    def test_replacement_symlink_cannot_redirect_the_reader(self):
        self.markdown.write_text("Configured content", encoding="utf-8")
        client = self.client()
        elsewhere = self.root / "unconfigured-private.txt"
        elsewhere.write_text("Do not disclose this content", encoding="utf-8")
        self.markdown.unlink()
        self.markdown.symlink_to(elsewhere)
        result = self.announcement(client)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["markdown"], "")

    def test_announcement_uses_existing_cookie_authentication(self):
        self.markdown.write_text("Private workspace notes", encoding="utf-8")
        token = self.root / "token"
        token.write_text("temporary-announcement-test-token\n", encoding="utf-8")
        token.chmod(0o600)
        self.config["security"] = {"enabled": True, "token_file": "../token"}
        client = self.client()
        locked = client.get("/api/announcement")
        self.assertEqual(locked.status_code, 401)
        self.assertNotIn("Private workspace notes", locked.text)
        self.assertNotIn("private-announcement.md", locked.text)
        login = client.post("/api/auth/login", json={"token": "temporary-announcement-test-token"})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.announcement(client)["markdown"], "Private workspace notes")
        client.post("/api/auth/logout")
        self.assertEqual(client.get("/api/announcement").status_code, 401)

    def test_invalid_configuration_is_rejected_with_clear_field_errors(self):
        invalid = [
            ({"path": ""}, "path"), ({"path": 123}, "path"),
            ({"path": "x.md", "title": ""}, "title"),
            ({"path": "x.md", "title": "two\nlines"}, "title"),
            ({"path": "x.md", "refresh_seconds": 0}, "refresh_seconds"),
            ({"path": "x.md", "refresh_seconds": 3601}, "refresh_seconds"),
            ({"path": "x.md", "refresh_seconds": True}, "refresh_seconds"),
            ({"path": "x.md", "refresh_seconds": float("nan")}, "refresh_seconds"),
        ]
        for announcement, field in invalid:
            with self.subTest(announcement=announcement):
                self.config["display"]["announcement"] = announcement
                self.config_path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, field):
                    create_app(self.config_path)


if __name__ == "__main__":
    unittest.main()
