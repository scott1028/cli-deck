"""The detail screen strips escape sequences.

Textual's Log only strips \\x00-\\x14, so ESC from the raw pty bytes leaks to
the real terminal: watch's ESC[1;24r breaks the dashboard's scroll region and
its repaint loses the header row. DetailScreen.feed must strip escape
sequences before Log.write.
"""
import asyncio
import base64
import unittest
from pathlib import Path

from textual.widgets import Log

from cli_deck.app import DeckApp, DetailScreen

PROC1 = {"id": "abc123", "name": "watch date", "argv": ["bash", "-ic", "watch date"],
         "cwd": "/tmp", "state": "running", "exit_code": None,
         "started_at": 1759000000.0, "log_path": ""}

# what a full-screen child like watch emits: alt screen, DECSTBM, OSC 4
# palette, cursor home, clear, then visible text
WATCH_BYTES = (b"\x1b[?1049h\x1b[1;24r\x1b]4;8;rgb:54/54/54\x1b\\"
               b"\x1b[H\x1b[2Jhello\r\n")


class FakeClient:
    """Stand-in for DeckClient: records sends, feeds canned messages."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.messages: asyncio.Queue = asyncio.Queue()

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)


class DetailFeedEscapeTest(unittest.IsolatedAsyncioTestCase):
    async def test_feed_strips_escape_sequences_from_attach_replay(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)

            await client.messages.put(
                {"msg": "attached", "id": "abc123",
                 "replay": base64.b64encode(WATCH_BYTES).decode()})
            await pilot.pause()

            joined = "\n".join(app.screen.query_one(Log).lines)
            self.assertNotIn("\x1b", joined)
            self.assertIn("hello", joined)

    async def test_feed_strips_escape_sequences_from_live_log(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            await client.messages.put(
                {"msg": "log", "id": "abc123",
                 "data": base64.b64encode(WATCH_BYTES).decode()})
            await pilot.pause()

            joined = "\n".join(app.screen.query_one(Log).lines)
            self.assertNotIn("\x1b", joined)
            self.assertIn("hello", joined)


if __name__ == "__main__":
    unittest.main()
