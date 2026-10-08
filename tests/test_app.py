import asyncio
import base64
import unittest
from pathlib import Path

from textual.widgets import DataTable

from cli_deck.app import DeckApp, DetailScreen, PromptScreen

PROC1 = {"id": "abc123", "name": "bash -i", "argv": ["bash", "-i"], "cwd": "/tmp",
         "state": "running", "exit_code": None, "started_at": 1759000000.0,
         "log_path": ""}
PROC2 = {"id": "def456", "name": "vim", "argv": ["vim"], "cwd": "/tmp",
         "state": "running", "exit_code": None, "started_at": 1759000001.0,
         "log_path": ""}
SNAPSHOT = {"msg": "snapshot", "processes": [PROC1]}


class FakeClient:
    """Stand-in for DeckClient: records sends, feeds canned messages."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.messages: asyncio.Queue = asyncio.Queue()

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)


class AppSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_table_add_detail_takeover_and_detach(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        takeovers: list[str] = []
        app.takeover_process = takeovers.append  # suspend needs a real tty

        async with app.run_test() as pilot:
            await client.messages.put(SNAPSHOT)
            await pilot.pause()

            table = app.query_one(DataTable)
            self.assertEqual(table.row_count, 1)
            self.assertIn("abc123", table.get_row_at(0))

            # a=add opens the prompt; cancel leaves it untouched
            await pilot.press("a")
            await pilot.pause()
            self.assertIsInstance(app.screen, PromptScreen)
            await pilot.press("escape")
            await pilot.pause()

            # enter=detail: read-only follow, attaches and syncs winsize
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            self.assertIn({"cmd": "attach", "id": "abc123"}, client.sent)
            winsizes = [m for m in client.sent
                        if m["cmd"] == "winsize" and m["id"] == "abc123"]
            self.assertTrue(winsizes)
            self.assertGreater(winsizes[0]["rows"], 0)
            self.assertGreater(winsizes[0]["cols"], 0)

            # detail is read-only: typed characters are NOT forwarded
            await pilot.press("e", "c", "h", "o")
            await pilot.pause()
            self.assertFalse([m for m in client.sent if m["cmd"] == "attach_input"])

            # t=takeover from the detail screen
            await pilot.press("t")
            await pilot.pause()
            self.assertEqual(takeovers, ["abc123"])

            # escape is the child's key: forwarded to the pty, not navigation
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            self.assertIn({"cmd": "attach_input", "id": "abc123",
                           "data": base64.b64encode(b"\x1b").decode()},
                          client.sent)

            # b leaves detail, unsubscribing first
            await pilot.press("b")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, DetailScreen)
            self.assertIn({"cmd": "attach", "id": None}, client.sent)

            # x=kill sends the kill command for the selected row
            await pilot.press("x")
            await pilot.pause()
            self.assertIn({"cmd": "kill", "id": "abc123"}, client.sent)

            # d=detach exits the TUI without touching the daemon
            await pilot.press("d")
        self.assertNotIn({"cmd": "stop"}, client.sent)

    async def test_takeover_from_list_screen(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        takeovers: list[str] = []
        app.takeover_process = takeovers.append
        async with app.run_test() as pilot:
            await client.messages.put(SNAPSHOT)
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            self.assertEqual(takeovers, ["abc123"])

    async def test_cursor_survives_snapshot_updates(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put(
                {"msg": "snapshot", "processes": [PROC1, PROC2]})
            await pilot.pause()
            table = app.query_one(DataTable)
            self.assertEqual(table.row_count, 2)

            await pilot.press("down")
            self.assertEqual(table.cursor_row, 1)

            # state change on the selected row must not move the cursor
            exited = {**PROC2, "state": "exited", "exit_code": 0}
            await client.messages.put(
                {"msg": "snapshot", "processes": [PROC1, exited]})
            await pilot.pause()
            self.assertEqual(table.cursor_row, 1)
            self.assertEqual(table.get_row_at(1)[2], "exited")
            self.assertEqual(table.get_row_at(1)[3], "0")

            # removal keeps the surviving row and a sane cursor
            await client.messages.put({"msg": "snapshot", "processes": [exited]})
            await pilot.pause()
            self.assertEqual(table.row_count, 1)
            self.assertEqual(table.get_row_at(0)[0], "def456")
            self.assertLess(table.cursor_row, table.row_count)

    async def test_sends_are_ordered(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test():
            for i in range(20):
                app.client_send({"cmd": "list", "seq": i})
            await asyncio.sleep(0.5)
        seqs = [m["seq"] for m in client.sent if "seq" in m]
        self.assertEqual(seqs, list(range(20)))


if __name__ == "__main__":
    unittest.main()
