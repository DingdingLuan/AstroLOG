"""Native SSH transport checks using disposable keys and a loopback server."""

import asyncio
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import asyncssh
import yaml

from logpresent import collector
from logpresent.remote_collect import Reader, inventory


class AsyncSSHTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astrolog-native-ssh-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / "remote"
        (self.remote / "output").mkdir(parents=True)
        self.source = self.remote / "output" / "info.txt"
        self.source.write_bytes(b"first\n")
        self.cache = self.root / "cache"
        self.connections = []
        self.handlers = set()
        self.requests = []
        self.silent = False
        self.stop = asyncio.Event()
        self.task = None
        host_key = asyncssh.generate_private_key("ssh-ed25519")
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        self.client_key = self.root / "client key"
        client_key.write_private_key(self.client_key)
        expected_key = client_key.export_public_key()
        owner = self

        class Server(asyncssh.SSHServer):
            def connection_made(self, conn):
                owner.connections.append(conn)

            def begin_auth(self, username):
                return True

            def public_key_auth_supported(self):
                return True

            def validate_public_key(self, username, key):
                return username == "tester" and key.export_public_key() == expected_key

        self.server = await asyncssh.create_server(
            Server, "127.0.0.1", 0, server_host_keys=[host_key],
            process_factory=self.serve, encoding=None,
        )
        self.port = self.server.get_port()
        self.known_hosts = self.root / "known hosts"
        self.known_hosts.write_text(
            f"[127.0.0.1]:{self.port} " + host_key.export_public_key().decode().strip() + "\n"
        )
        self.document = {
            "ssh": {"host": "127.0.0.1", "port": self.port, "user": "tester",
                    "identity_file": str(self.client_key), "known_hosts": str(self.known_hosts)},
            "remote_root": str(self.remote), "local_root": str(self.cache),
            "logs": {"output/info.txt": "output/info.txt"},
            "poll_seconds": .2, "inventory_seconds": 5,
            "inventory_patterns": ["output/*.txt"],
        }
        self.path = self.root / "collector.yaml"
        self.path.write_text(yaml.safe_dump(self.document))
        self.config = collector.load_collector_config(self.path)
        timing = patch.multiple(collector, RETRY_INITIAL=.02, RETRY_MAX=.05,
                                CLOSE_TIMEOUT=.5, CONNECT_TIMEOUT=2)
        timing.start()
        self.addCleanup(timing.stop)

    async def asyncTearDown(self):
        await self.stop_collector()
        for connection in self.connections:
            connection.close()
        for connection in self.connections:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.wait_closed(), 1)
        self.server.close()
        await self.server.wait_closed()
        handlers = list(self.handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)

    async def serve(self, process):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            document = json.loads(shlex.split(process.command)[-1])
            self.requests.append(json.loads(json.dumps(document)))
            reader = Reader(document["remote_root"], document["logs"], document.get("resume"))
            first = True
            while not process.is_closing():
                if not self.silent:
                    events = reader.poll()
                    if first:
                        events.append({"event": "inventory", "manifest": inventory(
                            self.remote, document.get("inventory_patterns", []))})
                        first = False
                    events.append({"event": "heartbeat", "at": datetime.now(timezone.utc).isoformat()})
                    for event in events:
                        process.stdout.write(json.dumps(event).encode() + b"\n")
                    await process.stdout.drain()
                await asyncio.sleep(.01)
        except (asyncio.CancelledError, asyncssh.Error, OSError):
            pass
        finally:
            self.handlers.discard(task)
            with contextlib.suppress(Exception):
                process.exit(0)

    async def start_collector(self):
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(collector.run_async(self.config, self.stop))

    async def stop_collector(self):
        self.stop.set()
        if self.task is not None:
            await asyncio.wait_for(self.task, 3)
            self.task = None

    async def wait_for(self, predicate, timeout=4):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self.task is not None and self.task.done():
                self.task.result()
                self.fail("Collector stopped before the expected event")
            try:
                if predicate():
                    return
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("Timed out waiting for the native SSH collector")
            await asyncio.sleep(.01)

    def replica(self):
        return (self.cache / "output/info.txt").read_bytes()

    def status(self):
        return json.loads((self.cache / "collector-status.json").read_text())

    async def test_native_stream_append_metadata_and_replacement_without_system_ssh(self):
        with patch("subprocess.Popen", side_effect=AssertionError("External SSH is not allowed")):
            await self.start_collector()
            await self.wait_for(lambda: self.replica() == b"first\n" and self.status()["connected"])
            self.assertTrue(self.status()["connected"])
            self.assertNotIn("ssh", self.requests[0])
            self.assertNotIn("local_root", self.requests[0])
            with self.source.open("ab") as output:
                output.write("第二行\n".encode())
            await self.wait_for(lambda: self.replica() == self.source.read_bytes())
            # One base64 frame exceeds the usual 64 KiB readline limit.
            with self.source.open("ab") as output:
                output.write(b"large frame " + b"x" * 150_000 + b"\n")
            await self.wait_for(lambda: self.replica() == self.source.read_bytes())
            original = (self.cache / "output/info.txt").stat().st_ino
            self.source.write_bytes(b"rewritten and longer than before\n")
            await self.wait_for(lambda: self.replica() == self.source.read_bytes())
            self.assertNotEqual((self.cache / "output/info.txt").stat().st_ino, original)
            self.source.rename(self.source.with_suffix(".old"))
            self.source.write_bytes(b"rotated\n")
            await self.wait_for(lambda: self.replica() == b"rotated\n")
            await self.wait_for(lambda: (self.cache / "inventory.json").exists())
            self.assertEqual(json.loads((self.cache / "inventory.json").read_text())["version"], 1)
            await self.stop_collector()
            self.assertFalse(self.status()["connected"])
            self.assertEqual(self.status()["state"], "stopped")

    async def test_connection_loss_and_process_restart_resume_saved_offsets(self):
        with self.assertLogs("astrolog.collector", level="WARNING"):
            await self.start_collector()
            await self.wait_for(lambda: self.replica() == b"first\n")
            self.connections[-1].abort()
            with self.source.open("ab") as output:
                output.write(b"after disconnect\n")
            await self.wait_for(lambda: len(self.requests) >= 2 and self.replica() == self.source.read_bytes())
            self.assertEqual(self.requests[1]["resume"]["output/info.txt"]["offset"], len(b"first\n"))
            await self.stop_collector()
        with self.source.open("ab") as output:
            output.write(b"while stopped\n")
        await self.start_collector()
        await self.wait_for(lambda: self.replica() == self.source.read_bytes())
        self.assertEqual(self.replica().count(b"after disconnect\n"), 1)
        self.assertEqual(self.replica().count(b"while stopped\n"), 1)

    async def test_unknown_host_is_rejected_instead_of_disabling_host_key_checks(self):
        self.known_hosts.write_text("")
        with self.assertLogs("astrolog.collector", level="WARNING"):
            await self.start_collector()
            await self.wait_for(lambda: bool(self.status().get("last_error")))
            self.assertFalse(self.status()["connected"])
            self.assertFalse((self.cache / "output/info.txt").exists())
            self.assertEqual(self.requests, [])
            await self.stop_collector()

    async def test_heartbeat_timeout_reconnects_and_duplicate_writer_is_rejected(self):
        self.silent = True
        with patch.object(collector, "HEARTBEAT_TIMEOUT", .08), \
                self.assertLogs("astrolog.collector", level="WARNING"):
            await self.start_collector()
            await self.wait_for(lambda: len(self.requests) >= 2)
            self.assertFalse(self.status()["connected"])
            with self.assertRaisesRegex(ValueError, "collector|writing|lock"):
                await collector.run_async(self.config, asyncio.Event())
            self.silent = False
            await self.wait_for(lambda: self.replica() == b"first\n")
            await self.stop_collector()
