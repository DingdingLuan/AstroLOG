"""SSH collector protocol checks against disposable local files only."""

import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

import yaml

from logpresent.collector import Mirror, load_collector_config, ssh_command
from logpresent.remote_collect import Reader, inventory, relative_path


class CollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astrolog-collector-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.remote = self.base / "remote"
        self.remote.mkdir()
        (self.remote / "output").mkdir()
        self.local = self.base / "replica"
        self.logs = {"output/info.txt": "output/info.txt"}
        self.config = {"ssh_hops": ["relay", "cluster"], "remote_root": str(self.remote),
                       "local_root": str(self.local), "logs": self.logs}
        self.source = self.remote / "output" / "info.txt"

    def mirror(self):
        return Mirror(self.config)

    def reader(self, mirror=None):
        resume = json.loads(json.dumps(mirror.states)) if mirror else None
        return Reader(self.remote, self.logs, resume)

    def pump(self, reader, mirror):
        events = reader.poll()
        for event in events:
            mirror.accept(event)
        return events

    def test_missing_logs_wait_without_creating_sample_content(self):
        mirror = self.mirror()
        self.pump(self.reader(), mirror)
        self.assertFalse((self.local / "output" / "info.txt").exists())
        self.assertEqual(mirror.status["state"], "waiting_for_logs")
        self.assertFalse(mirror.status["files"]["output/info.txt"]["exists"])

    def test_append_and_reconnect_resume_without_duplicates(self):
        self.source.write_bytes(b"first\n")
        mirror = self.mirror()
        self.pump(self.reader(), mirror)
        with self.source.open("ab") as stream:
            stream.write("第二行\n".encode())
        resumed_mirror = self.mirror()
        resumed = self.reader(resumed_mirror)
        events = self.pump(resumed, resumed_mirror)
        self.assertFalse(any(event["event"] == "reset" for event in events))
        self.assertEqual((self.local / "output" / "info.txt").read_bytes(), self.source.read_bytes())
        self.assertEqual(resumed_mirror.states["output/info.txt"]["offset"], self.source.stat().st_size)

    def test_replayed_chunk_is_idempotent_and_does_not_truncate_newer_data(self):
        self.source.write_bytes(b"one\n")
        mirror, reader = self.mirror(), self.reader()
        first = self.pump(reader, mirror)
        replay = next(event for event in first if event["event"] == "data")
        with self.source.open("ab") as stream:
            stream.write(b"two\n")
        self.pump(reader, mirror)
        mirror.accept(replay)
        self.assertEqual((self.local / "output" / "info.txt").read_bytes(), b"one\ntwo\n")
        self.assertEqual(mirror.states["output/info.txt"]["offset"], 8)

    def test_rotation_and_same_inode_regrowth_replace_replica_generation(self):
        self.source.write_bytes(b"original\n")
        mirror, reader = self.mirror(), self.reader()
        self.pump(reader, mirror)
        target = self.local / "output" / "info.txt"
        original_inode = target.stat().st_ino
        self.source.write_bytes(b"different and longer\n")
        events = self.pump(reader, mirror)
        self.assertTrue(any(event["event"] == "reset" for event in events))
        self.assertNotEqual(target.stat().st_ino, original_inode)
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        self.source.rename(self.source.with_suffix(".old"))
        self.source.write_bytes(b"fresh\n")
        self.assertTrue(any(event["event"] == "reset" for event in self.pump(reader, mirror)))
        self.assertEqual(target.read_bytes(), b"fresh\n")

    def test_glob_switches_to_latest_file(self):
        logs = {"logs/stdout.log": "output/run-*.out"}
        self.config["logs"] = logs
        old = self.remote / "output" / "run-1.out"
        new = self.remote / "output" / "run-2.out"
        old.write_bytes(b"previous job\n")
        reader, mirror = Reader(self.remote, logs), self.mirror()
        self.pump(reader, mirror)
        new.write_bytes(b"new job\n")
        os.utime(new, ns=(old.stat().st_mtime_ns + 10_000_000, old.stat().st_mtime_ns + 10_000_000))
        self.pump(reader, mirror)
        self.assertEqual((self.local / "logs" / "stdout.log").read_bytes(), b"new job\n")

    def test_paths_and_symlinks_cannot_escape_allowlisted_roots(self):
        for path in ("../outside", "/etc/passwd", "a/../../b", "folder/*/run.log", "x\\y"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                relative_path(path, allow_glob=True)
        outside = self.base / "private.txt"
        outside.write_bytes(b"unrelated\n")
        self.source.symlink_to(outside)
        mirror = self.mirror()
        events = self.pump(self.reader(), mirror)
        self.assertFalse(any(event["event"] == "data" for event in events))
        self.assertFalse((self.local / "output" / "info.txt").exists())
        (self.local / "output").symlink_to(self.base)
        with self.assertRaises(ValueError):
            mirror.target("output/info.txt")

    def test_inventory_reports_real_sizes_and_partial_scan_limits(self):
        snapshot = self.remote / "output" / "snapdir_000"
        snapshot.mkdir()
        (snapshot / "snap.0.hdf5").write_bytes(b"abc")
        (snapshot / "snap.1.hdf5").write_bytes(b"12345")
        manifest = inventory(self.remote, ["output/snapdir_*"])
        rows = {row["path"]: row for row in manifest["entries"]}
        self.assertEqual(rows["output/snapdir_000"]["size_bytes"], 8)
        self.assertTrue(rows["output/snapdir_000"]["size_known"])
        self.assertFalse(rows["output"]["size_known"])
        limited = inventory(self.remote, ["output/snapdir_*"], max_entries=2)
        self.assertTrue(limited["truncated"])
        self.assertLessEqual(len(limited["entries"]), 2)
        self.assertFalse(next(row for row in limited["entries"] if row["is_dir"])["size_known"])

    def test_config_and_ssh_quoting_use_existing_aliases_without_shell_injection(self):
        path = self.base / "collector.yaml"
        path.write_text(yaml.safe_dump({**self.config, "local_root": "replicas"}))
        config = load_collector_config(path)
        self.assertEqual(config["local_root"], str(self.base / "replicas"))
        command = ssh_command(config, {})
        self.assertEqual(command[0], "ssh")
        self.assertIn("relay", command)
        self.assertIn("cluster", command[-1])
        self.assertIn("BatchMode=yes", command)
        config["ssh_hops"] = ["--evil"]
        path.write_text(yaml.safe_dump(config))
        with self.assertRaises(ValueError):
            load_collector_config(path)

    def test_direct_ssh_configuration_resolves_key_paths_and_builds_explicit_arguments(self):
        key = self.base / "client key"
        certificate = self.base / "client key-cert.pub"
        key.write_text("disposable test key placeholder")
        certificate.write_text("disposable test certificate placeholder")
        path = self.base / "collector.yaml"
        document = {name: value for name, value in self.config.items() if name != "ssh_hops"}
        document["ssh"] = {"host": "2001:db8::1", "user": "test-user", "port": 2222,
                           "identity_file": key.name, "certificate_file": certificate.name}
        path.write_text(yaml.safe_dump(document))
        config = load_collector_config(path)
        self.assertEqual(config["ssh"]["identity_file"], str(key))
        self.assertEqual(config["ssh"]["certificate_file"], str(certificate))
        command = ssh_command(config, {})
        self.assertEqual(command[command.index("-p") + 1], "2222")
        self.assertEqual(command[command.index("-l") + 1], "test-user")
        self.assertEqual(command[command.index("-i") + 1], str(key))
        self.assertEqual(command[-2], "2001:db8::1")
        self.assertIn('CertificateFile=' + json.dumps(str(certificate)), command)
        remote = json.loads(shlex.split(command[-1])[-1])
        self.assertNotIn("ssh", remote)
        self.assertNotIn("ssh_hops", remote)
        self.assertNotIn("local_root", remote)
        if shutil.which("ssh"):
            # ssh -G parses all options without opening a network connection.
            parsed = subprocess.run([command[0], "-G", *command[1:-1]],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            settings = dict(row.split(" ", 1) for row in parsed.stdout.splitlines() if " " in row)
            self.assertEqual(settings["hostname"], "2001:db8::1")
            self.assertEqual(settings["user"], "test-user")
            self.assertEqual(settings["port"], "2222")
            self.assertEqual(settings["certificatefile"], str(certificate))

    def test_direct_ssh_defaults_allow_existing_agent_or_default_identity(self):
        path = self.base / "collector.yaml"
        document = {name: value for name, value in self.config.items() if name != "ssh_hops"}
        document["ssh"] = {"host": "cluster.example"}
        path.write_text(yaml.safe_dump(document))
        config = load_collector_config(path)
        self.assertEqual(config["ssh"], {"host": "cluster.example", "port": 22})
        command = ssh_command(config, {})
        self.assertEqual(command[command.index("-p") + 1], "22")
        self.assertNotIn("-l", command)
        self.assertNotIn("-i", command)

    def test_direct_ssh_rejects_invalid_connection_options_and_missing_keys(self):
        path = self.base / "collector.yaml"
        document = {name: value for name, value in self.config.items() if name != "ssh_hops"}
        invalid = [None, [], {}, {"host": "host", "password": "unsupported"}]
        invalid += [{"host": host} for host in ("", "-oProxyCommand=bad", "host name", "http://host",
                                                "user@host", "host\n", "::1%bad scope")]
        invalid += [{"host": "host", "port": port} for port in (0, 65536, True, "22", None)]
        invalid += [{"host": "host", "user": user} for user in ("", "-bad", "bad\nuser", "bad user")]
        invalid += [{"host": "host", key: value} for key in ("identity_file", "certificate_file")
                    for value in ("", "missing-file", "bad\npath", str(self.base), None)]
        for value in invalid:
            with self.subTest(connection=value):
                document["ssh"] = value
                path.write_text(yaml.safe_dump(document))
                with self.assertRaisesRegex(ValueError, "ssh"):
                    load_collector_config(path)
        document["ssh"] = {"host": "host"}
        document["ssh_hops"] = ["cluster"]
        path.write_text(yaml.safe_dump(document))
        with self.assertRaisesRegex(ValueError, "either ssh or ssh_hops"):
            load_collector_config(path)
