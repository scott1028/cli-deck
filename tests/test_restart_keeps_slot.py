"""Restart keeps the row slot.

After `r` restart the new row (new id, new started_at) must sit where the old
row was: the daemon registry replaces in place, and the TUI follows snapshot
(registry) order with the cursor staying on the same index.
"""
import asyncio
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from textual.widgets import DataTable

from cli_deck.app import DeckApp
from cli_deck.deckd import DeckClient, DeckDaemon


async def _await_msg(client: DeckClient, kind: str) -> dict:
    while True:
        msg = await client.messages.get()
        if msg.get("msg") == kind:
            return msg


class DaemonRestartSlotTest(unittest.TestCase):
    def _run_session(self, session) -> None:
        """Start a daemon on a temp socket, run an async client session."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sock = tmp_path / "deck.sock"
            daemon = DeckDaemon(sock, tmp_path / "deck.json", tmp_path / "logs")
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=self._run_daemon,
                                      args=(daemon, loop), daemon=True)
            thread.start()
            try:
                self._wait_for_socket(sock)
                asyncio.run(session(sock, tmp_path))
            finally:
                thread.join(timeout=15)
                if loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)
                    thread.join(timeout=5)

    @staticmethod
    def _run_daemon(daemon: DeckDaemon, loop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(daemon.run())
        finally:
            loop.close()

    @staticmethod
    def _wait_for_socket(sock: Path) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(sock))
            except OSError:
                time.sleep(0.05)
            else:
                probe.close()
                return
        raise TimeoutError("daemon socket never became connectable")

    # -- helpers ------------------------------------------------------------

    @staticmethod
    async def _add_three(client: DeckClient) -> list[str]:
        ids = []
        for label in ("A", "B", "C"):
            await client.send({"cmd": "add", "command": "sleep 30",
                               "name": label})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            ids = [p["id"] for p in snap["processes"]]
        assert len(ids) == 3
        return ids

    async def _poll_replaced(self, client: DeckClient, gone_id: str,
                             timeout: float = 20.0) -> dict | None:
        """Poll until gone_id is gone and three running rows remain, else None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            procs = snap["processes"]
            if (len(procs) == 3 and all(p["id"] != gone_id for p in procs)
                    and all(p["state"] == "running" for p in procs)):
                return snap
            await asyncio.sleep(0.2)
        return None

    # -- tests --------------------------------------------------------------

    def test_restart_exited_keeps_row_position(self):
        self._run_session(self._restart_exited_slot_session)

    async def _restart_exited_slot_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            a_id, b_id, c_id = await self._add_three(client)

            await client.send({"cmd": "kill", "id": b_id})
            deadline = time.monotonic() + 20
            exited = False
            while time.monotonic() < deadline and not exited:
                await client.send({"cmd": "list"})
                snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
                exited = any(p["id"] == b_id and p["state"] == "exited"
                             for p in snap["processes"])
            self.assertTrue(exited, "B never reached exited")

            await client.send({"cmd": "restart", "id": b_id})
            snap = await self._poll_replaced(client, b_id)
            self.assertIsNotNone(snap, "expected a respawned running row")
            procs = snap["processes"]
            self.assertEqual([p["name"] for p in procs], ["A", "B", "C"])
            new_id = procs[1]["id"]
            self.assertNotEqual(new_id, b_id)
            self.assertEqual([p["id"] for p in procs], [a_id, new_id, c_id])
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_restart_running_keeps_row_position(self):
        self._run_session(self._restart_running_slot_session)

    async def _restart_running_slot_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            a_id, b_id, c_id = await self._add_three(client)

            await client.send({"cmd": "restart", "id": b_id})
            snap = await self._poll_replaced(client, b_id)
            self.assertIsNotNone(snap, "expected a respawned running row")
            procs = snap["processes"]
            # the new row sits where B was, not at the end
            self.assertEqual([p["name"] for p in procs], ["A", "B", "C"])
            new_id = procs[1]["id"]
            self.assertNotEqual(new_id, b_id)
            self.assertEqual([p["id"] for p in procs], [a_id, new_id, c_id])
            await client.send({"cmd": "stop"})
        finally:
            await client.close()


def _proc(pid: str, name: str, started: float) -> dict:
    return {"id": pid, "name": name, "argv": ["bash", "-i"], "cwd": "/tmp",
            "state": "running", "exit_code": None, "started_at": started,
            "log_path": ""}


A = _proc("a1", "job-a", 1759000000.0)
B1 = _proc("b1", "job-b", 1759000001.0)
C1 = _proc("c1", "job-c", 1759000002.0)
# restarted rows carry a fresh (latest) started_at but must keep the slot
B2 = _proc("b2", "job-b", 1759000009.0)
C2 = _proc("c2", "job-c", 1759000010.0)


class FakeClient:
    """Stand-in for DeckClient: records sends, feeds canned messages."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.messages: asyncio.Queue = asyncio.Queue()

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)


class TuiRestartSlotTest(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_on_last_row_follows_restart(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot",
                                       "processes": [A, B1, C1]})
            await pilot.pause()
            table = app.query_one(DataTable)
            await pilot.press("down", "down")  # cursor on C (last row)
            self.assertEqual(table.cursor_row, 2)

            await client.messages.put({"msg": "snapshot",
                                       "processes": [A, B1, C2]})
            await pilot.pause()
            self.assertEqual(table.cursor_row, 2)
            self.assertEqual(table.get_row_at(table.cursor_row)[0], "c2")

    async def test_cursor_stays_at_index_when_row_removed(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot",
                                       "processes": [A, B1, C1]})
            await pilot.pause()
            table = app.query_one(DataTable)
            await pilot.press("down")  # cursor on B
            self.assertEqual(table.cursor_row, 1)

            # B removed: the cursor stays at index 1, now C
            await client.messages.put({"msg": "snapshot",
                                       "processes": [A, C1]})
            await pilot.pause()
            self.assertEqual(table.cursor_row, 1)
            self.assertEqual(table.get_row_at(table.cursor_row)[0], "c1")

    async def test_table_follows_snapshot_order_cursor_follows_row(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot",
                                       "processes": [A, B1, C1]})
            await pilot.pause()
            table = app.query_one(DataTable)
            self.assertEqual([table.get_row_at(i)[0] for i in range(3)],
                             ["a1", "b1", "c1"])
            await pilot.press("down")  # cursor on B
            self.assertEqual(table.cursor_row, 1)

            # restart snapshot: b2 is newest but sits in b1's slot, and the
            # cursor stays on that index (now the new row)
            await client.messages.put({"msg": "snapshot",
                                       "processes": [A, B2, C1]})
            await pilot.pause()
            self.assertEqual([table.get_row_at(i)[0] for i in range(3)],
                             ["a1", "b2", "c1"])
            self.assertEqual(table.cursor_row, 1)
            self.assertEqual(table.get_row_at(table.cursor_row)[0], "b2")


if __name__ == "__main__":
    unittest.main()
