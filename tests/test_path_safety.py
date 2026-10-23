"""Descriptor traversal stays within the configured tree during path replacement."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from logpresent.logs import FileTracker, LogError


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astrolog-path-safety-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "source"
        self.nested = self.root / "nested"
        self.nested.mkdir(parents=True)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.log = self.nested / "events.log"
        self.original = b"allowed log\n"
        self.log.write_bytes(self.original)
        (self.outside / "events.log").write_bytes(b"unconfigured synthetic content\n")
        self.tracker = FileTracker(self.log, "example.events", self.root)

    def replace_parent(self):
        self.nested.rename(self.root / "original-nested")
        self.nested.symlink_to(self.outside, target_is_directory=True)

    def test_parent_symlink_is_rejected_for_read_and_metadata(self):
        self.replace_parent()
        for operation in (self.tracker.tail, self.tracker.snapshot):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(LogError) as caught:
                    operation()
                self.assertEqual(caught.exception.status_code, 403)

    def test_parent_replaced_before_descent_is_rejected(self):
        real_open = os.open
        replaced = False

        def replacing_open(path, flags, *args, **kwargs):
            nonlocal replaced
            if path == "nested" and kwargs.get("dir_fd") is not None and not replaced:
                replaced = True
                self.replace_parent()
            return real_open(path, flags, *args, **kwargs)

        with patch("logpresent.logs.os.open", side_effect=replacing_open):
            with self.assertRaises(LogError) as caught:
                self.tracker.tail()
        self.assertTrue(replaced)
        self.assertEqual(caught.exception.status_code, 403)

    def test_parent_replaced_after_descent_cannot_redirect_read(self):
        real_open = os.open
        replaced = False

        def replacing_open(path, flags, *args, **kwargs):
            nonlocal replaced
            if path == "events.log" and kwargs.get("dir_fd") is not None and not replaced:
                replaced = True
                self.replace_parent()
            return real_open(path, flags, *args, **kwargs)

        with patch("logpresent.logs.os.open", side_effect=replacing_open):
            result = self.tracker.tail()
        self.assertTrue(replaced)
        self.assertEqual([row["text"] for row in result["lines"]], ["allowed log"])

    def test_parent_replacement_cannot_redirect_snapshot_metadata(self):
        real_open = os.open
        replaced = False

        def replacing_open(path, flags, *args, **kwargs):
            nonlocal replaced
            if path == "events.log" and kwargs.get("dir_fd") is not None and not replaced:
                replaced = True
                self.replace_parent()
            return real_open(path, flags, *args, **kwargs)

        with patch("logpresent.logs.os.open", side_effect=replacing_open):
            snapshot = self.tracker.snapshot()
        self.assertTrue(replaced)
        self.assertEqual(snapshot.size_bytes, len(self.original))

    def test_replaced_root_ancestor_cannot_redirect_read(self):
        # Replace an ancestor of source.root, after its original descriptor was
        # opened. Walking only source.root as one full path would follow it.
        holder = self.base / "holder"
        holder.mkdir()
        self.root.rename(holder / "source")
        tracker = FileTracker(holder / "source/nested/events.log", "example.events", holder / "source")
        other_holder = self.base / "other-holder"
        (other_holder / "source/nested").mkdir(parents=True)
        (other_holder / "source/nested/events.log").write_text("unconfigured\n")
        real_open = os.open
        replaced = False

        def replacing_open(path, flags, *args, **kwargs):
            nonlocal replaced
            if path == "source" and kwargs.get("dir_fd") is not None and not replaced:
                replaced = True
                holder.rename(self.base / "original-holder")
                holder.symlink_to(other_holder, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        with patch("logpresent.logs.os.open", side_effect=replacing_open):
            result = tracker.tail()
        self.assertTrue(replaced)
        self.assertEqual([row["text"] for row in result["lines"]], ["allowed log"])

    def test_final_symlink_and_lexical_escape_are_rejected(self):
        self.log.unlink()
        self.log.symlink_to(self.outside / "events.log")
        for operation in (self.tracker.tail, self.tracker.snapshot):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(LogError) as caught:
                    operation()
                self.assertIn(caught.exception.status_code, (400, 403))
        outside = FileTracker(self.outside / "events.log", "outside", self.root)
        with self.assertRaises(LogError) as caught:
            outside.tail()
        self.assertEqual(caught.exception.status_code, 403)

    def test_initial_parent_alias_is_canonicalized_once(self):
        alias = self.base / "source-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        tracker = FileTracker(alias / "nested/events.log", "example.events", alias)
        self.assertEqual(tracker.tail()["lines"][0]["text"], "allowed log")
        alias.unlink()
        alias.symlink_to(self.outside, target_is_directory=True)
        self.assertEqual(tracker.tail()["lines"][0]["text"], "allowed log")


if __name__ == "__main__":
    unittest.main()
