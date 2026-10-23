"""Optional cookie authentication, exercised only against disposable sources."""

from __future__ import annotations

from http.cookies import SimpleCookie
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import yaml

from logpresent.app import create_app


class AuthenticationTests(unittest.TestCase):
    token = "temporary-test-passphrase-0feef29258a64087"
    cookie_name = "logpresent_session"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="logpresent-auth-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.marker = "PRIVATE-LOG-CONTENT-4f740783"
        (self.root / "private.log").write_text(self.marker + "\n", encoding="utf-8")
        self.token_path = self.root / "access.token"
        self.token_path.write_text(self.token + "\n", encoding="utf-8")
        self.token_path.chmod(0o600)
        self.config_path = self.root / "config.yaml"
        self.config = {
            "display": {"title": "Private workspace marker", "default_file": "private.main"},
            "data": {"sources": {"private": {
                "type": "local", "root": str(self.root),
                "files": {"main": {"path": "private.log", "label": "Private data"}},
                "inventory": {"patterns": ["*.log"]},
            }}},
            "security": {"enabled": True, "token_file": "access.token", "session_days": 7, "secure_cookie": False},
        }
        self.client = self.new_client()

    def new_client(self, **kwargs):
        self.config_path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        client = TestClient(create_app(self.config_path), **kwargs)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def login(self, client=None):
        client = client or self.client
        response = client.post("/api/auth/login", json={"token": self.token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["authenticated"])
        return response

    def cookie_value(self, response):
        cookies = SimpleCookie()
        cookies.load(response.headers["set-cookie"])
        return cookies[self.cookie_name].value

    def test_all_data_apis_and_api_documentation_require_authentication(self):
        endpoints = [
            "/api/config", "/api/files", "/api/summary?file_id=private.main",
            "/api/inventory?source=private",
            "/api/folders?source=private",
            "/api/logs/private.main/tail",
            "/api/logs/private.main/window?percent=50",
            "/api/logs/private.main/index",
            "/api/logs/private.main/forward?after=0&generation=unknown",
            "/api/logs/private.main/download",
            "/api/logs/private.main/history?before=0&generation=unknown",
            "/api/logs/private.main/stream?cursor=0&generation=unknown",
            "/api/docs", "/api/openapi.json",
        ]
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertNotIn(self.marker, response.text)
                self.assertNotIn(str(self.root), response.text)

    def test_public_shell_status_and_health_do_not_embed_private_data(self):
        status = self.client.get("/api/auth/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json(), {"enabled": True, "authenticated": False})
        for endpoint in ("/", "/app.js", "/styles.css", "/healthz"):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(self.marker, response.text)
                self.assertNotIn(self.token, response.text)
                self.assertNotIn(str(self.root), response.text)
                self.assertNotIn("Private workspace marker", response.text)

    def test_bad_token_denied_then_valid_cookie_unlocks_data(self):
        wrong = self.client.post("/api/auth/login", json={"token": "wrong-passphrase"})
        self.assertEqual(wrong.status_code, 401)
        self.assertNotIn(self.cookie_name, wrong.headers.get("set-cookie", ""))
        response = self.login()
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertIn("path=/", cookie)
        self.assertNotIn(self.token, response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/auth/status").json(), {"enabled": True, "authenticated": True})
        tail = self.client.get("/api/logs/private.main/tail")
        self.assertEqual(tail.status_code, 200)
        self.assertIn(self.marker, tail.text)

    def test_tampered_cookie_cannot_read_data(self):
        cookie = self.cookie_value(self.login())
        self.client.cookies.clear()
        response = self.client.get("/api/files", headers={"Cookie": f"{self.cookie_name}={cookie}tampered"})
        self.assertEqual(response.status_code, 401)

    def test_expired_signed_cookie_is_rejected(self):
        cookie = self.cookie_value(self.login())
        self.client.cookies.clear()
        with patch("logpresent.auth.time.time", return_value=4_000_000_000):
            response = self.client.get("/api/files", headers={"Cookie": f"{self.cookie_name}={cookie}"})
        self.assertEqual(response.status_code, 401, response.text)

    def test_logout_clears_cookie_and_relocks_data(self):
        self.login()
        logout = self.client.post("/api/auth/logout")
        self.assertEqual(logout.status_code, 200, logout.text)
        self.assertIn("max-age=0", logout.headers["set-cookie"].lower())
        self.assertEqual(self.client.get("/api/files").status_code, 401)
        self.assertFalse(self.client.get("/api/auth/status").json()["authenticated"])

    def test_cookie_survives_restart_but_not_secret_rotation(self):
        signing_key = Path(str(self.token_path) + ".session-key")
        original_key = signing_key.read_bytes()
        self.assertEqual(len(original_key), 32)
        self.assertEqual(signing_key.stat().st_mode & 0o777, 0o600)
        cookie = self.cookie_value(self.login())
        restarted = self.new_client()
        self.assertEqual(signing_key.read_bytes(), original_key)
        self.assertEqual(restarted.get("/api/files", headers={"Cookie": f"{self.cookie_name}={cookie}"}).status_code, 200)
        self.token_path.write_text("rotated-test-passphrase-dfb50e3042da4f7a\n", encoding="utf-8")
        rotated = self.new_client()
        self.assertEqual(rotated.get("/api/files", headers={"Cookie": f"{self.cookie_name}={cookie}"}).status_code, 401)

    def test_cross_origin_login_rejected_and_same_origin_allowed(self):
        wrong_origin = self.client.post("/api/auth/login", json={"token": self.token}, headers={"Origin": "https://unrelated.invalid"})
        self.assertEqual(wrong_origin.status_code, 403, wrong_origin.text)
        same_origin = self.client.post("/api/auth/login", json={"token": self.token}, headers={"Origin": "http://testserver"})
        self.assertEqual(same_origin.status_code, 200, same_origin.text)

    def test_https_option_sets_secure_cookie(self):
        self.config["security"]["secure_cookie"] = True
        secure_client = self.new_client(base_url="https://testserver")
        response = self.login(secure_client)
        self.assertIn("; secure", response.headers["set-cookie"].lower())
        self.assertEqual(secure_client.get("/api/files").status_code, 200)

    def test_repeated_bad_tokens_are_rate_limited(self):
        responses = [self.client.post("/api/auth/login", json={"token": "wrong-passphrase"}) for _ in range(6)]
        self.assertEqual(responses[0].status_code, 401)
        self.assertEqual(responses[-1].status_code, 429, responses[-1].text)

    def test_authorized_download_preserves_exact_bytes_without_touching_source(self):
        source = self.root / "private.log"
        payload = b"literal <script>not HTML</script>\n\x00\xffunfinished"
        source.write_bytes(payload)
        initial_mtime = source.stat().st_mtime_ns
        self.assertEqual(self.client.get("/api/logs/private.main/download").status_code, 401)
        self.login()
        response = self.client.get("/api/logs/private.main/download")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, payload)
        self.assertEqual(int(response.headers["x-logpresent-snapshot-bytes"]), len(payload))
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(source.read_bytes(), payload)
        self.assertEqual(source.stat().st_mtime_ns, initial_mtime)


if __name__ == "__main__":
    unittest.main()
