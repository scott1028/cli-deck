"""Restart (`r`) and stop/remove (`x`).

Daemon sessions mirror tests/test_protocol.py; TUI sessions mirror tests/test_app.py.
"""
import asyncio
import base64
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from textual.widgets import DataTable, Static

from cli_deck.app import ConfirmScreen, DeckApp, DetailScreen
from cli_deck.deckd import DeckClient, DeckDaemon


async def _await_msg(client: DeckClient, kind: str) -> dict:
    while True:
        msg = await client.messages.get()
        if msg.get("msg") == kind:
            return msg


class DaemonRestartTest(unittest.TestCase):
    def _run_session(self, session) -> None:
        """Start a daemon on a temp socket, run an async client session."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sock = tmp_path / "deck.sock"
            logs = tmp_path / "logs"
            daemon = DeckDaemon(sock, tmp_path / "deck.json", logs)
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=self._run_daemon,
                                      args=(daemon, loop), daemon=True)
            thread.start()
            try:
                self._wait_for_socket(sock)
                asyncio.run(session(sock, tmp_path))
            finally:
                thread.join(timeout=15)  # stop cmd should end run() cleanly
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

    # -- tests --------------------------------------------------------------

    def test_terminate_first_remove_during_restart_removes_row(self):
        self._run_session(self._remove_cancels_restart_session)

    async def _remove_cancels_restart_session(self, sock: Path,
                                              tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            # traps keep the row in "stopping" long enough to act mid-kill;
            # the echo marks the moment the traps are actually installed
            await client.send({"cmd": "add",
                               "command": "trap '' INT TERM; echo up; "
                                          "while true; do sleep 1; done",
                               "name": "keeper"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            old_id = snap["processes"][0]["id"]
            await self._await_output(client, old_id, b"up")

            await client.send({"cmd": "restart", "id": old_id})
            await asyncio.wait_for(self._await_stopping(client), 10)

            # terminate-first remove wins over the queued restart: no new row
            await client.send({"cmd": "remove", "id": old_id,
                               "terminate_first": True})
            empty = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                await client.send({"cmd": "list"})
                snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
                if not snap["processes"]:
                    empty = snap
                    break
                await asyncio.sleep(0.2)
            self.assertIsNotNone(empty, "expected the row to be removed")
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_kill_during_pending_restart_cancels_respawn(self):
        self._run_session(self._kill_cancels_restart_session)

    async def _kill_cancels_restart_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            # traps keep the row in "stopping" long enough to act mid-kill;
            # the echo marks the moment the traps are actually installed
            await client.send({"cmd": "add",
                               "command": "trap '' INT TERM; echo up; "
                                          "while true; do sleep 1; done",
                               "name": "keeper"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            old_id = snap["processes"][0]["id"]
            await self._await_output(client, old_id, b"up")

            await client.send({"cmd": "restart", "id": old_id})
            snap = await asyncio.wait_for(self._await_stopping(client), 10)
            self.assertEqual(snap["processes"][0]["id"], old_id)

            # an explicit stop cancels the queued restart: exited, no new row
            await client.send({"cmd": "kill", "id": old_id})
            final = await self._poll_final(client, timeout=15)
            self.assertIsNotNone(final, "expected the row to end exited")
            self.assertEqual([p["id"] for p in final["processes"]], [old_id])
            self.assertEqual(final["processes"][0]["state"], "exited")
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    async def _await_stopping(self, client: DeckClient) -> dict:
        while True:
            snap = await _await_msg(client, "snapshot")
            if snap["processes"] and all(p["state"] == "stopping"
                                         for p in snap["processes"]):
                return snap

    @staticmethod
    async def _await_output(client: DeckClient, proc_id: str,
                            marker: bytes) -> None:
        """Attach and wait until the child has emitted `marker`."""
        await client.send({"cmd": "attach", "id": proc_id})
        data = b""
        while marker not in data:
            msg = await asyncio.wait_for(_await_msg(client, "log"), 20)
            if msg["id"] == proc_id:
                data += base64.b64decode(msg["data"])

    async def _poll_final(self, client: DeckClient, timeout: float = 10.0,
                          ) -> dict | None:
        """Poll until every row is exited (respawns never settle), else None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            if snap["processes"] and all(p["state"] == "exited"
                                         for p in snap["processes"]):
                return snap
            await asyncio.sleep(0.2)
        return None

    def test_restart_running_replaces_row_keeping_launch_info(self):
        self._run_session(self._restart_running_session)

    async def _restart_running_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add",
                               "command": 'echo "PID=$$ FOO=$FOO CWD=$PWD"; '
                                          'sleep 30',
                               "cwd": str(tmp), "env": {"FOO": "bar"}})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            old_id = snap["processes"][0]["id"]

            # capture the old child's pid from its live output
            await client.send({"cmd": "attach", "id": old_id})
            await asyncio.wait_for(_await_msg(client, "attached"), 5)
            data = b""
            while b"PID=" not in data:
                msg = await asyncio.wait_for(_await_msg(client, "log"), 20)
                data += base64.b64decode(msg["data"])
            old_pid = int(re.search(rb"PID=(\d+)", data).group(1))

            await client.send({"cmd": "restart", "id": old_id})
            snap = await self._poll_replaced(client, old_id, timeout=20)
            self.assertIsNotNone(snap, "expected a respawned running row")
            new = snap["processes"][0]
            self.assertNotEqual(new["id"], old_id)

            # the new row replays its own output: same env overlay and cwd,
            # but a different child pid
            await client.send({"cmd": "attach", "id": new["id"]})
            attached = await asyncio.wait_for(_await_msg(client, "attached"), 5)
            data = base64.b64decode(attached["replay"])
            while b"PID=" not in data:
                msg = await asyncio.wait_for(_await_msg(client, "log"), 20)
                if msg["id"] == new["id"]:
                    data += base64.b64decode(msg["data"])
            self.assertIn(b"FOO=bar", data)
            self.assertIn(f"CWD={tmp}".encode(), data)
            new_pid = int(re.search(rb"PID=(\d+)", data).group(1))
            self.assertNotEqual(new_pid, old_pid)

            # the old raw log file stays on disk
            self.assertTrue((tmp / "logs" / f"{old_id}.log").exists())
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_restart_exited_respawns_immediately(self):
        self._run_session(self._restart_exited_session)

    async def _restart_exited_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "echo first",
                               "name": "job"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            old_id = snap["processes"][0]["id"]
            snap = await asyncio.wait_for(self._await_exited(client), 20)
            self.assertEqual(snap["processes"][0]["id"], old_id)

            await client.send({"cmd": "restart", "id": old_id})
            snap = await self._poll_replaced(client, old_id)
            self.assertIsNotNone(snap, "expected a respawned running row")
            new = snap["processes"][0]
            self.assertNotEqual(new["id"], old_id)
            self.assertEqual(new["name"], "job")
            self.assertEqual(new["state"], "running")
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    @staticmethod
    async def _await_exited(client: DeckClient) -> dict:
        while True:
            snap = await _await_msg(client, "snapshot")
            if snap["processes"] and all(p["state"] == "exited"
                                         for p in snap["processes"]):
                return snap

    async def _poll_replaced(self, client: DeckClient, gone_id: str,
                             timeout: float = 10.0) -> dict | None:
        """Poll until gone_id is gone and one running row remains, else None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            procs = snap["processes"]
            if (procs and all(p["id"] != gone_id for p in procs)
                    and all(p["state"] == "running" for p in procs)):
                return snap
            await asyncio.sleep(0.2)
        return None

    def test_restart_unknown_id_errors_and_connection_stays_usable(self):
        self._run_session(self._unknown_id_session)

    async def _unknown_id_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "restart", "id": "nope"})
            err = await asyncio.wait_for(_await_msg(client, "error"), 5)
            self.assertIn("no such process", err["message"])
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(snap["processes"], [])
            await client.send({"cmd": "stop"})
        finally:
            await client.close()


PROC1 = {"id": "abc123", "name": "bash -i", "argv": ["bash", "-i"], "cwd": "/tmp",
         "state": "running", "exit_code": None, "started_at": 1759000000.0,
         "log_path": ""}
PROC2 = {"id": "def456", "name": "vim", "argv": ["vim"], "cwd": "/tmp",
         "state": "running", "exit_code": None, "started_at": 1759000001.0,
         "log_path": ""}


class FakeClient:
    """Stand-in for DeckClient: records sends, feeds canned messages."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.messages: asyncio.Queue = asyncio.Queue()

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)


class TuiRestartKeyTest(unittest.IsolatedAsyncioTestCase):
    async def test_r_without_selection_or_on_detail_does_nothing(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            # empty table: r notifies and sends nothing
            await client.messages.put({"msg": "snapshot", "processes": []})
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            self.assertFalse([m for m in client.sent if m["cmd"] == "restart"])

            # detail screen open: r must not touch the row behind it
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            await pilot.press("r")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            self.assertFalse([m for m in client.sent if m["cmd"] == "restart"])

    async def test_r_on_running_asks_first_default_no(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()

            await pilot.press("r")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)

            # default No: Enter cancels, nothing is sent
            await pilot.press("enter")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ConfirmScreen)
            self.assertFalse([m for m in client.sent if m["cmd"] == "restart"])

            # Escape cancels too
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse([m for m in client.sent if m["cmd"] == "restart"])

            # Yes sends restart for the row captured when the dialog opened
            await pilot.press("r")
            await pilot.pause()
            other = {**PROC2, "state": "exited", "exit_code": 0}
            await client.messages.put({"msg": "snapshot", "processes": [other]})
            await pilot.pause()
            await pilot.press("shift+tab", "enter")
            await pilot.pause()
            self.assertIn({"cmd": "restart", "id": "abc123"}, client.sent)

    async def test_r_on_exited_sends_restart_without_dialog(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            exited = {**PROC1, "state": "exited", "exit_code": 0}
            await client.messages.put({"msg": "snapshot", "processes": [exited]})
            await pilot.pause()

            await pilot.press("r")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ConfirmScreen)
            self.assertIn({"cmd": "restart", "id": "abc123"}, client.sent)


class TuiKillKeyTest(unittest.IsolatedAsyncioTestCase):
    async def test_x_on_detail_screen_does_nothing(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)

            # x must not touch the row hidden behind the detail screen
            await pilot.press("x")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            self.assertFalse([m for m in client.sent
                              if m["cmd"] in ("kill", "remove")])

    async def test_x_on_exited_removes_directly_without_confirm(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            exited = {**PROC1, "state": "exited", "exit_code": 0}
            await client.messages.put({"msg": "snapshot", "processes": [exited]})
            await pilot.pause()

            # no dialog: x on an exited row removes it right away
            await pilot.press("x")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ConfirmScreen)
            self.assertIn({"cmd": "remove", "id": "abc123",
                           "terminate_first": False}, client.sent)

    async def test_x_on_stopping_sends_kill_directly(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            stopping = {**PROC1, "state": "stopping"}
            await client.messages.put({"msg": "snapshot",
                                       "processes": [stopping]})
            await pilot.pause()

            await pilot.press("x")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ConfirmScreen)
            self.assertIn({"cmd": "kill", "id": "abc123"}, client.sent)

    async def test_x_on_running_asks_stop_then_kills(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()

            await pilot.press("x")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            question = str(app.screen.query_one("#confirm-question",
                                                Static).render())
            self.assertIn("Stop 'bash -i'", question)

            # default No: Enter cancels
            await pilot.press("enter")
            await pilot.pause()
            self.assertFalse([m for m in client.sent if m["cmd"] == "kill"])

            # Yes sends kill; the row itself stays (daemon keeps exited rows)
            await pilot.press("x")
            await pilot.pause()
            await pilot.press("shift+tab", "enter")
            await pilot.pause()
            self.assertIn({"cmd": "kill", "id": "abc123"}, client.sent)
            table = app.query_one(DataTable)
            self.assertEqual(table.row_count, 1)


class ConfirmDialogArrowsTest(unittest.IsolatedAsyncioTestCase):
    async def test_left_right_focus_yes_no_and_stop_at_bounds(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            # q's daemon-stop dialog: default No, arrows move focus
            await pilot.press("q")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            self.assertEqual(app.focused.id, "no")

            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(app.focused.id, "yes")
            await pilot.press("left")  # boundary: stays on Yes, no cycling
            await pilot.pause()
            self.assertEqual(app.focused.id, "yes")
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app.focused.id, "no")
            await pilot.press("right")  # boundary: stays on No
            await pilot.pause()
            self.assertEqual(app.focused.id, "no")
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIn({"cmd": "stop"}, client.sent)

            # x's stop dialog: left then Enter really sends the kill
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            await pilot.press("left", "enter")
            await pilot.pause()
            self.assertIn({"cmd": "kill", "id": "abc123"}, client.sent)


class HelpBarTest(unittest.IsolatedAsyncioTestCase):
    async def test_help_bar_lists_stop_remove_and_restart(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test():
            bar = str(app.query_one("#help-bar", Static).render())
        self.assertIn("x stop/remove", bar)
        self.assertIn("r restart", bar)


if __name__ == "__main__":
    unittest.main()

