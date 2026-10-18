"""Quitting an empty dashboard.

Once the first snapshot has arrived and the dashboard has no rows, `q` stops
the daemon and exits without a confirm: nothing can be killed. Before the
first snapshot the empty table only means data has not arrived yet, so `q`
still asks.
"""
import asyncio
import unittest
from pathlib import Path

from cli_deck.app import ConfirmScreen, DeckApp

PROC1 = {"id": "abc123", "name": "bash -i", "argv": ["bash", "-i"], "cwd": "/tmp",
         "state": "running", "exit_code": None, "started_at": 1759000000.0,
         "log_path": ""}


class FakeClient:
    """Stand-in for DeckClient: records sends, feeds canned messages."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.messages: asyncio.Queue = asyncio.Queue()

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)


class QuitEmptyTest(unittest.IsolatedAsyncioTestCase):
    async def test_q_with_exited_row_still_asks(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            exited = {**PROC1, "state": "exited", "exit_code": 0}
            await client.messages.put({"msg": "snapshot", "processes": [exited]})
            await pilot.pause()

            await pilot.press("q")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            self.assertNotIn({"cmd": "stop"}, client.sent)

    async def test_q_before_first_snapshot_still_asks(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            # empty only because the first snapshot has not arrived yet
            await pilot.press("q")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            self.assertNotIn({"cmd": "stop"}, client.sent)

    async def test_q_on_empty_dashboard_stops_daemon_without_confirm(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": []})
            await pilot.pause()

            await pilot.press("q")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ConfirmScreen)
            await asyncio.sleep(0.4)  # _stop_and_exit waits 0.2s before exit
        self.assertIn({"cmd": "stop"}, client.sent)
        self.assertEqual(app.return_code, 0)


if __name__ == "__main__":
    unittest.main()
