"""SSH collector protocol checks against disposable local files only."""

import base64
import json
import os
from pathlib import Path
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
