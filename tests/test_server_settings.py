"""Server binding and override precedence, using only temporary configuration."""

import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from logpresent.__main__ import main
from logpresent.config import ServerSettings, Settings, load_config, resolve_server


class ServerSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astrolog-server-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "run.log").write_text("Synthetic example\n", encoding="utf-8")
        self.path = self.root / "config.yaml"
        self.document = {"data": {"sources": {"demo": {
            "root": str(self.root), "files": {"main": "run.log"},
        }}}}

    def write_config(self):
        self.path.write_text(yaml.safe_dump(self.document), encoding="utf-8")
        return self.path

    def test_existing_configs_and_positional_settings_keep_default_binding(self):
        original = load_config(self.write_config())
        self.assertEqual(original.server, ServerSettings("127.0.0.1", 8765))
        positional = Settings(original.display, original.sources, original.files,
                              original.poll_seconds, original.security, original.announcement)
        self.assertEqual(positional.server, original.server)
        for block in (None, {}):
            with self.subTest(block=block):
                self.document["server"] = block
                self.assertEqual(load_config(self.write_config()).server, original.server)

    def test_yaml_custom_host_port_and_partial_blocks(self):
        for block, expected in (
            ({"host": " 0.0.0.0 ", "port": 9080}, ServerSettings("0.0.0.0", 9080)),
            ({"host": "localhost"}, ServerSettings("localhost", 8765)),
            ({"port": 65535}, ServerSettings("127.0.0.1", 65535)),
            ({"host": "::1", "port": 1}, ServerSettings("::1", 1)),
        ):
            with self.subTest(block=block):
                self.document["server"] = block
                self.assertEqual(load_config(self.write_config()).server, expected)

    def test_yaml_invalid_bindings_fail_before_startup(self):
        invalid = [[], "localhost", 1, {"ports": 9080}]
        invalid += [{"host": value} for value in (
            "", "   ", None, 123, "http://localhost", "localhost:9080",
            "/tmp/socket", "bad host", "[::1]", "host\n", "a\\b", "a@b",
        )]
        invalid += [{"port": value} for value in (
            0, -1, 65536, True, None, 9080.0, "9080",
        )]
        for block in invalid:
            with self.subTest(block=block):
                self.document["server"] = block
                with self.assertRaisesRegex(ValueError, "server"):
                    load_config(self.write_config())

    def test_override_precedence_is_independent_for_each_field(self):
        configured = ServerSettings("yaml.example", 9080)
        environment = {"LOGPRESENT_HOST": "env.example", "LOGPRESENT_PORT": "9090"}
        self.assertEqual(resolve_server(configured, environ={}), configured)
        self.assertEqual(resolve_server(configured, environ=environment),
                         ServerSettings("env.example", 9090))
        self.assertEqual(resolve_server(configured, host="cli.example", environ=environment),
                         ServerSettings("cli.example", 9090))
        self.assertEqual(resolve_server(configured, port=9100, environ=environment),
                         ServerSettings("env.example", 9100))
        self.assertEqual(resolve_server(configured, environ={
            "LOGPRESENT_HOST": "", "LOGPRESENT_PORT": ""}), configured)
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(resolve_server(configured), ServerSettings("env.example", 9090))

    def test_invalid_environment_is_rejected_only_when_used(self):
        configured = ServerSettings()
        for environment in (
            {"LOGPRESENT_HOST": "http://localhost"},
            {"LOGPRESENT_PORT": "0"}, {"LOGPRESENT_PORT": "65536"},
            {"LOGPRESENT_PORT": "9.5"}, {"LOGPRESENT_PORT": "nine"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaises(ValueError):
                    resolve_server(configured, environ=environment)
        invalid = {"LOGPRESENT_HOST": "http://localhost", "LOGPRESENT_PORT": "invalid"}
        self.assertEqual(resolve_server(configured, host="127.0.0.1", port=9123,
                                        environ=invalid), ServerSettings("127.0.0.1", 9123))

    def test_cli_uses_yaml_and_explicit_overrides(self):
        self.document["server"] = {"host": "0.0.0.0", "port": 9080}
        self.write_config()
        for options, environment, expected in (
            ([], {}, ("0.0.0.0", 9080)),
            ([], {"LOGPRESENT_PORT": "9090"}, ("0.0.0.0", 9090)),
            (["--host", "127.0.0.1", "--port", "9100"],
             {"LOGPRESENT_HOST": "bad host", "LOGPRESENT_PORT": "invalid"},
             ("127.0.0.1", 9100)),
        ):
            with self.subTest(options=options, environment=environment):
                argv = ["astrolog", "--config", str(self.path), *options]
                with patch("sys.argv", argv), patch.dict(os.environ, environment, clear=True), \
                        patch("logpresent.__main__.uvicorn.run") as run:
                    main()
                run.assert_called_once()
                self.assertEqual(run.call_args.kwargs["host"], expected[0])
                self.assertEqual(run.call_args.kwargs["port"], expected[1])
                self.assertEqual(run.call_args.args[0].state.settings.server,
                                 ServerSettings("0.0.0.0", 9080))

    def test_cli_reports_invalid_binding_without_starting_uvicorn(self):
        self.write_config()
        for options, environment in (
            (["--port", "0"], {}), (["--port", "65536"], {}),
            (["--host", "http://localhost"], {}),
            ([], {"LOGPRESENT_PORT": "not-a-port"}),
        ):
            with self.subTest(options=options, environment=environment):
                error = io.StringIO()
                argv = ["astrolog", "--config", str(self.path), *options]
                with patch("sys.argv", argv), patch.dict(os.environ, environment, clear=True), \
                        patch("logpresent.__main__.uvicorn.run") as run, \
                        contextlib.redirect_stderr(error):
                    with self.assertRaises(SystemExit) as raised:
                        main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("error:", error.getvalue())
                run.assert_not_called()
