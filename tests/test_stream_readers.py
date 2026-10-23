"""Streaming cursor checks with temporary files and bounded async waits."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from logpresent.app import stream_events
from logpresent.logs import FileTracker, LogError


def event_data(event):
    payload = event["data"]
    return json.loads(payload) if isinstance(payload, str) else payload


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="logpresent-reader-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "example.log"
        self.path.write_text("initial\n", encoding="utf-8")
        self.tracker = FileTracker(self.path)

    def test_cursor_replay_excludes_initial_tail_and_delivers_append_once(self):
        initial = self.tracker.tail(100)
        with self.path.open("ab") as output:
            output.write("second 🌌\nthird\n".encode("utf-8"))
        first = self.tracker.read_new(initial["cursor"], initial["generation"])
        self.assertEqual([row["text"] for row in first["lines"]], ["second 🌌", "third"])
        self.assertEqual(first["lines"][0]["start"], initial["cursor"])
        self.assertEqual(first["cursor"], self.path.stat().st_size)
        repeated = self.tracker.read_new(first["cursor"], initial["generation"])
        self.assertEqual(repeated["lines"], [])
        self.assertEqual(repeated["cursor"], first["cursor"])

    def test_split_utf8_character_and_partial_line_are_withheld_then_completed(self):
        initial = self.tracker.tail(100)
        message = "new 🚀 complete".encode("utf-8")
        split = len("new ".encode("utf-8")) + 2
        with self.path.open("ab") as output:
            output.write(message[:split])
        pending = self.tracker.read_new(initial["cursor"], initial["generation"])
        self.assertEqual(pending["lines"], [])
        self.assertEqual(pending["cursor"], initial["cursor"])
        with self.path.open("ab") as output:
            output.write(message[split:] + b"\n")
        complete = self.tracker.read_new(pending["cursor"], initial["generation"])
        self.assertEqual([row["text"] for row in complete["lines"]], ["new 🚀 complete"])
        self.assertEqual(complete["cursor"], self.path.stat().st_size)

    def test_reading_log_never_changes_source_bytes_or_mtime(self):
        initial_bytes = self.path.read_bytes()
        initial_mtime = self.path.stat().st_mtime_ns
        tail = self.tracker.tail(100)
        self.tracker.history(tail["older_cursor"], 100, tail["generation"])
        self.tracker.read_new(tail["cursor"], tail["generation"])
        self.assertEqual(self.path.read_bytes(), initial_bytes)
        self.assertEqual(self.path.stat().st_mtime_ns, initial_mtime)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="logpresent-stream-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "example.log"
        self.path.write_text("initial\n", encoding="utf-8")
        self.tracker = FileTracker(self.path)

    async def next_kind(self, iterator, kind):
        async def search():
            async for event in iterator:
                if event["event"] == kind:
                    return event
            self.fail(f"Stream ended before {kind} event")

        return await asyncio.wait_for(search(), timeout=1.0)

    async def test_append_event_cursor_can_resume_without_duplicating_rows(self):
        initial = self.tracker.tail(100)
        with self.path.open("ab") as output:
            output.write(b"after connection\n")
        stream = stream_events(self.tracker, initial["cursor"], initial["generation"], 0.01)
        try:
            event = await self.next_kind(stream, "append")
        finally:
            await stream.aclose()
        payload = event_data(event)
        self.assertEqual([row["text"] for row in payload["lines"]], ["after connection"])
        self.assertEqual(event["id"], f"{initial['generation']}:{payload['cursor']}")
        with self.path.open("ab") as output:
            output.write(b"after reconnect\n")
        resumed = stream_events(self.tracker, payload["cursor"], initial["generation"], 0.01)
        try:
            replay = event_data(await self.next_kind(resumed, "append"))
        finally:
            await resumed.aclose()
        self.assertEqual([row["text"] for row in replay["lines"]], ["after reconnect"])

    async def test_replaced_file_emits_reset_instead_of_replaying_old_offset(self):
        initial = self.tracker.tail(100)
        self.path.rename(self.path.with_suffix(".old"))
        self.path.write_text("replacement file\n", encoding="utf-8")
        stream = stream_events(self.tracker, initial["cursor"], initial["generation"], 0.01)
        try:
            reset = event_data(await self.next_kind(stream, "reset"))
        finally:
            await stream.aclose()
        self.assertNotEqual(reset["generation"], initial["generation"])
        self.assertTrue(reset["reason"])

    async def test_expired_session_stops_stream_before_pending_data(self):
        initial = self.tracker.tail(100)
        with self.path.open("ab") as output:
            output.write(b"must not be sent after expiry\n")
        stream = stream_events(self.tracker, initial["cursor"], initial["generation"], 0.01, expires_at=0)
        try:
            event = await asyncio.wait_for(anext(stream), timeout=1.0)
            self.assertEqual(event["event"], "auth_expired")
            self.assertEqual(event_data(event), {"authenticated": False})
            with self.assertRaises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), timeout=1.0)
        finally:
            await stream.aclose()


if __name__ == "__main__":
    unittest.main()
