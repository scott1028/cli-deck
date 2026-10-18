"""The protection switch (`p`).

Daemon sessions mirror tests/test_protocol.py; TUI sessions mirror tests/test_app.py.
"""
import asyncio
import socket
import tempfile
import threading
import time
import unittest.mock
from pathlib import Path

from textual.widgets import DataTable, Static

from cli_deck import app as app_module
from cli_deck import deckd
from cli_deck.app import ConfirmScreen, DeckApp, DetailScreen
from cli_deck.deckd import DeckClient, DeckDaemon
from cli_deck.registry import Registry


async def _await_msg(client: DeckClient, kind: str) -> dict:
    while True:
        msg = await client.messages.get()
        if msg.get("msg") == kind:
            return msg


class RegistryProtectTest(unittest.TestCase):
    def test_to_public_exposes_protect_limit_only(self):
        record = Registry().create("x", ["x"], "/tmp")
        self.assertIn("protect_limit", record.to_public())
        record.protect_limit = 3
        record.quick_restarts = 2
        public = record.to_public()
        self.assertEqual(public["protect_limit"], 3)
        self.assertNotIn("quick_restarts", public)


class ProtectDaemonTest(unittest.TestCase):
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

    def test_protect_on_off_reflected_in_snapshot(self):
        self._run_session(self._protect_on_off_session)

    async def _protect_on_off_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 30",
                               "name": "keeper"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]

            await client.send({"cmd": "protect", "id": proc_id, "limit": 5})
            # the protect cmd itself broadcasts a snapshot before the notice
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(snap["processes"][0].get("protect_limit"), 5)
            notice = await asyncio.wait_for(_await_msg(client, "notice"), 5)
            self.assertIn("protection on", notice["message"])
            self.assertIn("keeper", notice["message"])

            await client.send({"cmd": "protect", "id": proc_id, "limit": None})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertIsNone(snap["processes"][0].get("protect_limit"))
            notice = await asyncio.wait_for(_await_msg(client, "notice"), 5)
            self.assertIn("protection off", notice["message"])
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_protect_bad_requests_error_and_connection_stays_usable(self):
        self._run_session(self._protect_bad_requests_session)

    async def _protect_bad_requests_session(self, sock: Path,
                                            tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "protect", "id": "nope", "limit": 5})
            err = await asyncio.wait_for(_await_msg(client, "error"), 5)
            self.assertIn("no such process", err["message"])

            await client.send({"cmd": "add", "command": "sleep 30",
                               "name": "keeper"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]

            # -2, a string and a bool are all rejected; the connection lives on
            for bad_limit in (-2, "5", True):
                await client.send({"cmd": "protect", "id": proc_id,
                                   "limit": bad_limit})
                err = await asyncio.wait_for(_await_msg(client, "error"), 5)
                self.assertIn("limit", err["message"])

            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(len(snap["processes"]), 1)
            self.assertIsNone(snap["processes"][0].get("protect_limit"))
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_protected_exit_on_its_own_respawns_in_place(self):
        self._run_session(self._auto_restart_session)

    async def _auto_restart_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 2; exit 3",
                               "name": "flaky"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            old = snap["processes"][0]
            await client.send({"cmd": "protect", "id": old["id"], "limit": 5,
                               "gap": 0.1})
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)

            snap, notices = await self._await_respawn(client, old["id"])
            self.assertIsNotNone(snap, "expected a protection respawn in place")
            # the gap notice trails the exited snapshot broadcast
            notices += await self._drain_notices(client)
            new = snap["processes"][0]
            self.assertNotEqual(new["id"], old["id"])
            self.assertEqual(new["name"], old["name"])
            self.assertEqual(new["argv"], old["argv"])
            self.assertEqual(new["state"], "running")
            self.assertEqual(new.get("protect_limit"), 5)
            self.assertTrue(any("protection restarts it in 0.1s" in n
                                for n in notices),
                            f"expected a gap notice, got {notices}")
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_protected_kill_stays_exited_without_respawn(self):
        self._run_session(self._protected_kill_session)

    async def _protected_kill_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 30",
                               "name": "keeper"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]
            await client.send({"cmd": "protect", "id": proc_id, "limit": 5})
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)

            await client.send({"cmd": "kill", "id": proc_id})
            final = await self._poll_final(client)
            self.assertIsNotNone(final, "expected the row to end exited")
            self.assertEqual([p["id"] for p in final["processes"]], [proc_id])
            # protection stays on the row: only tool-initiated stops skip it
            self.assertEqual(final["processes"][0].get("protect_limit"), 5)

            # a protected exited row must not come back on its own
            await asyncio.sleep(1.0)
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual([p["id"] for p in snap["processes"]], [proc_id])
            self.assertEqual(snap["processes"][0]["state"], "exited")
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_protected_terminate_first_remove_removes_row(self):
        self._run_session(self._protected_remove_session)

    async def _protected_remove_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 30",
                               "name": "keeper"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]
            await client.send({"cmd": "protect", "id": proc_id, "limit": 5})
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)

            await client.send({"cmd": "remove", "id": proc_id,
                               "terminate_first": True})
            self.assertTrue(await self._poll_empty(client),
                            "expected the row to be removed")
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_protected_restart_respawns_exactly_once(self):
        self._run_session(self._protected_restart_session)

    async def _protected_restart_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 30",
                               "name": "keeper"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            old_id = snap["processes"][0]["id"]
            await client.send({"cmd": "protect", "id": old_id, "limit": 5})
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)

            await client.send({"cmd": "restart", "id": old_id})
            snap, _ = await self._await_respawn(client, old_id)
            self.assertIsNotNone(snap, "expected one respawned running row")
            new_id = snap["processes"][0]["id"]
            self.assertEqual(snap["processes"][0].get("protect_limit"), 5)

            # the restart path owns the respawn: no second one follows
            await asyncio.sleep(1.0)
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual([p["id"] for p in snap["processes"]], [new_id])
            self.assertEqual(snap["processes"][0]["state"], "running")
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_unprotected_exit_stays_exited(self):
        self._run_session(self._unprotected_exit_session)

    async def _unprotected_exit_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "exit 3",
                               "name": "plain"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]

            final = await self._poll_final(client)
            self.assertIsNotNone(final, "expected the row to end exited")
            self.assertEqual([p["id"] for p in final["processes"]], [proc_id])
            self.assertEqual(final["processes"][0]["exit_code"], 3)
            self.assertIsNone(final["processes"][0].get("protect_limit"))
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_quick_restart_limit_turns_protection_off(self):
        self._run_session(self._quick_limit_session)

    async def _quick_limit_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "exit 1",
                               "name": "flaky"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]

            # limit 2: two quick restarts are allowed, the third exit gives up
            await client.send({"cmd": "protect", "id": proc_id, "limit": 2,
                               "gap": 0.1})
            restarts = 0
            gave_up = False
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not gave_up:
                try:
                    msg = await asyncio.wait_for(client.messages.get(), 1.0)
                except TimeoutError:
                    continue
                if (msg.get("msg") == "notice"
                        and "protection restarts it" in msg.get("message", "")):
                    restarts += 1
                elif (msg.get("msg") == "error"
                        and "protection turned off" in msg.get("message", "")):
                    gave_up = True
            self.assertTrue(gave_up, "expected the give-up error")
            self.assertEqual(restarts, 2)

            final = await self._poll_final(client)
            self.assertIsNotNone(final, "expected the row to stay exited")
            self.assertEqual(len(final["processes"]), 1)
            self.assertEqual(final["processes"][0]["state"], "exited")
            self.assertIsNone(final["processes"][0].get("protect_limit"))
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_unlimited_limit_keeps_respawning_until_turned_off(self):
        self._run_session(self._unlimited_session)

    async def _unlimited_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "exit 1",
                               "name": "flaky"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]

            await client.send({"cmd": "protect", "id": proc_id, "limit": -1,
                               "gap": 0.1})
            restarts = 0
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and restarts < 6:
                try:
                    msg = await asyncio.wait_for(client.messages.get(), 1.0)
                except TimeoutError:
                    continue
                if (msg.get("msg") == "notice"
                        and "protection restarts it" in msg.get("message", "")):
                    restarts += 1
            self.assertGreaterEqual(restarts, 6,
                                    "unlimited protection must keep restarting")

            self.assertTrue(await self._turn_off_protection(client),
                            "expected the protection off notice")
            final = await self._poll_final(client)
            self.assertIsNotNone(final, "expected the row to stay exited")
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_slow_exit_restarts_even_with_limit_zero(self):
        # threshold patched below the child's runtime: the exit is not "quick",
        # so the count resets and even limit 0 restarts
        with unittest.mock.patch.object(deckd, "PROTECT_QUICK_EXIT", 0.2,
                                        create=True):
            self._run_session(self._slow_exit_session)

    async def _slow_exit_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 0.5; exit 1",
                               "name": "slow"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            old_id = snap["processes"][0]["id"]
            await client.send({"cmd": "protect", "id": old_id, "limit": 0,
                               "gap": 0.1})
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)

            snap, _ = await self._await_respawn(client, old_id)
            self.assertIsNotNone(snap, "expected a restart after a slow exit")
            self.assertEqual(snap["processes"][0].get("protect_limit"), 0)
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_stop_with_protected_process_leaves_cleanly(self):
        self._run_session(self._stop_with_protected_session)

    async def _stop_with_protected_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 30",
                               "name": "keeper"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]
            await client.send({"cmd": "protect", "id": proc_id, "limit": 5})
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await asyncio.wait_for(_await_msg(client, "notice"), 5)

            # q: the daemon must tear down, not respawn the protected child
            await client.send({"cmd": "stop"})
            notice = await asyncio.wait_for(_await_msg(client, "notice"), 10)
            self.assertIn("daemon closed the connection", notice["message"])
        finally:
            await client.close()

    def test_protect_gap_default_validation_and_off(self):
        self._run_session(self._gap_settings_session)

    async def _gap_settings_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 30",
                               "name": "keeper"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]

            # gap omitted: the default 5.0, and the notice names it
            await client.send({"cmd": "protect", "id": proc_id, "limit": 5})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(snap["processes"][0].get("protect_gap"), 5.0)
            self.assertFalse(snap["processes"][0].get("protect_pending"))
            notice = await asyncio.wait_for(_await_msg(client, "notice"), 5)
            self.assertIn("restart gap: 5s", notice["message"])

            await client.send({"cmd": "protect", "id": proc_id, "limit": 5,
                               "gap": 0.5})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(snap["processes"][0].get("protect_gap"), 0.5)

            # 0.05, a string, a bool and NaN (json.loads accepts it) are all
            # rejected; the connection lives on
            for bad_gap in (0.05, "1", True, float("nan")):
                await client.send({"cmd": "protect", "id": proc_id,
                                   "limit": 5, "gap": bad_gap})
                err = await asyncio.wait_for(_await_msg(client, "error"), 5)
                self.assertIn("gap", err["message"])
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(snap["processes"][0].get("protect_gap"), 0.5)

            await client.send({"cmd": "protect", "id": proc_id, "limit": None})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertIsNone(snap["processes"][0].get("protect_gap"))
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_pending_restart_waits_gap_then_respawns(self):
        self._run_session(self._pending_wait_session)

    async def _pending_wait_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 0.3; exit 3",
                               "name": "flaky"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            old_id = snap["processes"][0]["id"]
            await client.send({"cmd": "protect", "id": old_id, "limit": 5,
                               "gap": 0.5})

            # during the gap the row stays exited, pending, with a notice
            notices: list[str] = []
            pending = await self._poll_pending(client, old_id, notices)
            self.assertIsNotNone(pending, "expected the row to go pending")
            self.assertTrue(any("in 0.5s" in n for n in notices),
                            f"expected a gap notice, got {notices}")

            snap, _ = await self._await_respawn(client, old_id)
            self.assertIsNotNone(snap, "expected the gap to fire a respawn")
            new = snap["processes"][0]
            self.assertEqual(new.get("protect_gap"), 0.5)
            self.assertFalse(new.get("protect_pending"))
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_protect_off_during_pending_cancels_restart(self):
        self._run_session(self._off_during_pending_session)

    async def _off_during_pending_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 0.3; exit 1",
                               "name": "flaky"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]
            await client.send({"cmd": "protect", "id": proc_id, "limit": 5,
                               "gap": 2.0})
            notices: list[str] = []
            pending = await self._poll_pending(client, proc_id, notices)
            self.assertIsNotNone(pending, "expected the row to go pending")

            await client.send({"cmd": "protect", "id": proc_id, "limit": None})
            # the off notice trails the protect broadcast: wait for it before
            # re-listing, so no stale snapshot can answer first
            notice = await asyncio.wait_for(_await_msg(client, "notice"), 5)
            self.assertIn("protection off", notice["message"])
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertIsNone(snap["processes"][0].get("protect_limit"))
            self.assertFalse(snap["processes"][0].get("protect_pending"))

            # past the gap: still the same exited row, no respawn
            await asyncio.sleep(2.5)
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual([p["id"] for p in snap["processes"]], [proc_id])
            self.assertEqual(snap["processes"][0]["state"], "exited")
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_remove_during_pending_removes_row(self):
        self._run_session(self._remove_during_pending_session)

    async def _remove_during_pending_session(self, sock: Path,
                                             tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 0.3; exit 1",
                               "name": "flaky"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]
            await client.send({"cmd": "protect", "id": proc_id, "limit": 5,
                               "gap": 2.0})
            notices: list[str] = []
            pending = await self._poll_pending(client, proc_id, notices)
            self.assertIsNotNone(pending, "expected the row to go pending")

            await client.send({"cmd": "remove", "id": proc_id})
            self.assertTrue(await self._poll_empty(client),
                            "expected the row to be removed")
            await asyncio.sleep(2.5)
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(snap["processes"], [])
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_restart_during_pending_respawns_once(self):
        self._run_session(self._restart_during_pending_session)

    async def _restart_during_pending_session(self, sock: Path,
                                              tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 0.3; exit 1",
                               "name": "flaky"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            old_id = snap["processes"][0]["id"]
            await client.send({"cmd": "protect", "id": old_id, "limit": 5,
                               "gap": 2.0})
            notices: list[str] = []
            pending = await self._poll_pending(client, old_id, notices)
            self.assertIsNotNone(pending, "expected the row to go pending")

            # r on the pending row: one immediate respawn, gap not elapsed
            await client.send({"cmd": "restart", "id": old_id})
            snap, _ = await self._await_respawn(client, old_id, timeout=5)
            self.assertIsNotNone(snap, "expected an immediate respawn")
            self.assertEqual(snap["processes"][0].get("protect_gap"), 2.0)

            # the canceled timer must not add a second row
            await asyncio.sleep(1.5)
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(len(snap["processes"]), 1)
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_invalid_protect_does_not_cancel_pending_restart(self):
        self._run_session(self._invalid_protect_pending_session)

    async def _invalid_protect_pending_session(self, sock: Path,
                                               tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "sleep 0.3; exit 1",
                               "name": "flaky"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            old_id = snap["processes"][0]["id"]
            await client.send({"cmd": "protect", "id": old_id, "limit": 5,
                               "gap": 1.0})
            notices: list[str] = []
            pending = await self._poll_pending(client, old_id, notices)
            self.assertIsNotNone(pending, "expected the row to go pending")

            # rejected protects must have no side effect: the scheduled
            # restart stays armed
            await client.send({"cmd": "protect", "id": old_id, "limit": -2,
                               "gap": 1.0})
            err = await asyncio.wait_for(_await_msg(client, "error"), 5)
            self.assertIn("limit", err["message"])
            await client.send({"cmd": "protect", "id": old_id, "limit": 5,
                               "gap": 0.05})
            err = await asyncio.wait_for(_await_msg(client, "error"), 5)
            self.assertIn("gap", err["message"])

            # past the gap: the restart still fires, protection kept
            snap, _ = await self._await_respawn(client, old_id)
            self.assertIsNotNone(snap, "expected the pending restart to fire")
            self.assertEqual(snap["processes"][0].get("protect_limit"), 5)
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_stop_during_pending_leaves_cleanly(self):
        self._run_session(self._stop_during_pending_session)

    async def _stop_during_pending_session(self, sock: Path,
                                           tmp: Path) -> None:
        runs = tmp / "runs"
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add",
                               "command": f"echo run >> {runs}; exit 1",
                               "name": "counter"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]
            # gap well past the teardown's bounded awaits: a timer that could
            # fire mid-teardown would mask the cancel we are checking here
            await client.send({"cmd": "protect", "id": proc_id, "limit": 5,
                               "gap": 5.0})
            notices: list[str] = []
            pending = await self._poll_pending(client, proc_id, notices)
            self.assertIsNotNone(pending, "expected the row to go pending")

            await client.send({"cmd": "stop"})
            closed = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not closed:
                try:
                    msg = await asyncio.wait_for(client.messages.get(), 1.0)
                except TimeoutError:
                    continue
                if (msg.get("msg") == "notice"
                        and "daemon closed" in msg.get("message", "")):
                    closed = True
            self.assertTrue(closed, "expected the daemon to close")

            # past the gap: the canceled timer must not have run the child again
            await asyncio.sleep(5.5)
            self.assertEqual(runs.read_text().count("run"), 1)
        finally:
            await client.close()

    # -- polling helpers -----------------------------------------------------

    async def _await_respawn(self, client: DeckClient, gone_id: str,
                             timeout: float = 15.0):
        """Poll list until gone_id is replaced by a running row; collect
        notices seen along the way. Returns (snapshot | None, notices)."""
        notices: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await client.send({"cmd": "list"})
            try:
                msg = await asyncio.wait_for(client.messages.get(), 5)
            except TimeoutError:
                continue
            if msg.get("msg") == "notice":
                notices.append(msg["message"])
            elif msg.get("msg") == "snapshot":
                procs = msg["processes"]
                if (procs and all(p["id"] != gone_id for p in procs)
                        and all(p["state"] == "running" for p in procs)):
                    return msg, notices
        return None, notices

    async def _poll_pending(self, client: DeckClient, proc_id: str,
                            notices: list[str], timeout: float = 15.0):
        """Poll list until proc_id is exited with protect_pending; collect
        notices along the way. Returns the matching snapshot, else None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await client.send({"cmd": "list"})
            try:
                msg = await asyncio.wait_for(client.messages.get(), 5)
            except TimeoutError:
                continue
            if msg.get("msg") == "notice":
                notices.append(msg["message"])
            elif msg.get("msg") == "snapshot":
                for proc in msg["processes"]:
                    if (proc["id"] == proc_id and proc["state"] == "exited"
                            and proc.get("protect_pending")):
                        return msg
            await asyncio.sleep(0.2)
        return None

    async def _drain_notices(self, client: DeckClient,
                             window: float = 1.0) -> list[str]:
        """Collect notices arriving within `window`, then return."""
        notices: list[str] = []
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(client.messages.get(),
                                             deadline - time.monotonic())
            except TimeoutError:
                break
            if msg.get("msg") == "notice":
                notices.append(msg["message"])
        return notices

    async def _poll_final(self, client: DeckClient,
                          timeout: float = 15.0) -> dict | None:
        """Poll until every row is exited, else None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            if snap["processes"] and all(p["state"] == "exited"
                                         for p in snap["processes"]):
                return snap
            await asyncio.sleep(0.2)
        return None

    async def _poll_empty(self, client: DeckClient, timeout: float = 15.0) -> bool:
        """Poll until the dashboard has no rows, else False."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            if not snap["processes"]:
                return True
            await asyncio.sleep(0.2)
        return False

    async def _turn_off_protection(self, client: DeckClient,
                                   timeout: float = 15.0) -> bool:
        """Send protect-null for the current row id, retrying over respawn
        churn, until the protection-off notice arrives."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            if not snap["processes"]:
                continue
            await client.send({"cmd": "protect",
                               "id": snap["processes"][0]["id"], "limit": None})
            try:
                msg = await asyncio.wait_for(client.messages.get(), 5)
            except TimeoutError:
                continue
            if (msg.get("msg") == "notice"
                    and "protection off" in msg.get("message", "")):
                return True
        return False


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


def _record_notifications(app: DeckApp) -> list[tuple[str, str]]:
    """Capture notify() calls as (message, severity) pairs."""
    notes: list[tuple[str, str]] = []

    def record(message: str, **kwargs) -> None:
        notes.append((message, str(kwargs.get("severity", "information"))))

    app.notify = record  # type: ignore[method-assign]
    return notes


class ParseProtectSettingsTest(unittest.TestCase):
    def test_defaults_and_typed_values(self):
        parse = getattr(app_module, "parse_protect_settings", None)
        self.assertIsNotNone(parse, "parse_protect_settings is missing")
        self.assertEqual(parse("", ""), (5, 5.0))
        self.assertEqual(parse("-1", "0.1"), (-1, 0.1))
        self.assertEqual(parse("3", "30"), (3, 30.0))

    def test_invalid_values_raise(self):
        parse = getattr(app_module, "parse_protect_settings", None)
        self.assertIsNotNone(parse, "parse_protect_settings is missing")
        for limit_text in ("abc", "-2"):
            with self.assertRaises(ValueError):
                parse(limit_text, "")
        for gap_text in ("abc", "0.05", "0", "-1", "nan", "inf"):
            with self.assertRaises(ValueError):
                parse("", gap_text)


class TuiProtectColumnTest(unittest.IsolatedAsyncioTestCase):
    async def test_protect_column_renders_all_states(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            # PROC1 has no protect keys at all: old snapshots still render
            await client.messages.put({"msg": "snapshot", "processes": [
                PROC1,
                {**PROC2, "id": "p5", "protect_limit": 5, "protect_gap": 5.0},
                {**PROC2, "id": "pm1", "protect_limit": -1, "protect_gap": 0.1},
                {**PROC2, "id": "ppd", "protect_limit": 5, "protect_gap": 5.0,
                 "protect_pending": True},
                {**PROC2, "id": "old", "protect_limit": 5}]})  # old daemon
            await pilot.pause()
            table = app.query_one(DataTable)
            self.assertEqual(len(table.get_row_at(0)), 6)
            self.assertEqual(table.get_row_at(0)[5], "")
            self.assertEqual(table.get_row_at(1)[5], "on (5, 5s)")
            self.assertEqual(table.get_row_at(2)[5], "on (∞, 0.1s)")
            self.assertEqual(table.get_row_at(3)[5], "restart in 5s")
            self.assertEqual(table.get_row_at(4)[5], "on (5)")

    async def test_protect_column_widens_on_in_place_update(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            # added unprotected: the column sits at its 7-char header width
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()
            table = app.query_one(DataTable)

            # same id updated in place: the cell must not clip to the header
            pending = {**PROC1, "protect_limit": 5, "protect_gap": 5.0,
                       "protect_pending": True}
            await client.messages.put({"msg": "snapshot",
                                       "processes": [pending]})
            await pilot.pause()
            self.assertEqual(table.get_row_at(0)[5], "restart in 5s")
            protect_column = list(table.columns.values())[5]
            self.assertGreaterEqual(protect_column.content_width,
                                    len("restart in 5s"))


class TuiProtectKeyTest(unittest.IsolatedAsyncioTestCase):
    async def test_p_on_unprotected_opens_two_field_screen_defaults(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()

            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(type(app.screen).__name__, "ProtectScreen")
            self.assertTrue(app.screen.query("#protect-limit"))
            self.assertTrue(app.screen.query("#protect-gap"))

            # both fields empty: the defaults limit 5, gap 5.0
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn({"cmd": "protect", "id": "abc123", "limit": 5,
                           "gap": 5.0}, client.sent)

    async def test_p_screen_tab_switches_fields_and_sends_values(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()

            await pilot.press("p")
            await pilot.pause()
            await pilot.press("-", "1", "tab", "0", ".", "1", "enter")
            await pilot.pause()
            self.assertIn({"cmd": "protect", "id": "abc123", "limit": -1,
                           "gap": 0.1}, client.sent)

            await pilot.press("p")
            await pilot.pause()
            await pilot.press("3", "tab", "3", "0", "enter")
            await pilot.pause()
            self.assertIn({"cmd": "protect", "id": "abc123", "limit": 3,
                           "gap": 30.0}, client.sent)

    async def test_p_screen_rejects_invalid_and_keeps_open(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        notes = _record_notifications(app)
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()

            await pilot.press("p")
            await pilot.pause()
            # gap below the minimum: error toast, dialog stays open, no send
            await pilot.press("tab", "0", ".", "0", "5", "enter")
            await pilot.pause()
            self.assertFalse([m for m in client.sent if m["cmd"] == "protect"])
            self.assertTrue(any(severity == "error" for _, severity in notes))
            self.assertEqual(type(app.screen).__name__, "ProtectScreen")

            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse([m for m in client.sent if m["cmd"] == "protect"])

    async def test_p_screen_escape_sends_nothing(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()

            await pilot.press("p")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotEqual(type(app.screen).__name__, "ProtectScreen")
            self.assertFalse([m for m in client.sent if m["cmd"] == "protect"])

    async def test_p_on_protected_turns_off_without_prompt(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            protected = {**PROC1, "protect_limit": 5, "protect_gap": 5.0}
            await client.messages.put({"msg": "snapshot",
                                       "processes": [protected]})
            await pilot.pause()

            await pilot.press("p")
            await pilot.pause()
            self.assertNotEqual(type(app.screen).__name__, "ProtectScreen")
            self.assertIn({"cmd": "protect", "id": "abc123", "limit": None},
                          client.sent)

    async def test_p_on_pending_row_turns_off_directly(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            pending = {**PROC1, "state": "exited", "exit_code": 1,
                       "protect_limit": 5, "protect_gap": 5.0,
                       "protect_pending": True}
            await client.messages.put({"msg": "snapshot",
                                       "processes": [pending]})
            await pilot.pause()

            await pilot.press("p")
            await pilot.pause()
            self.assertNotEqual(type(app.screen).__name__, "ProtectScreen")
            self.assertIn({"cmd": "protect", "id": "abc123", "limit": None},
                          client.sent)

    async def test_p_on_detail_or_modal_does_nothing(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()

            # detail screen open: p must not touch the row behind it
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            await pilot.press("p")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            self.assertFalse([m for m in client.sent if m["cmd"] == "protect"])

            # modal open (x's confirm): p must not touch the hidden row either
            await pilot.press("b")
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            await pilot.press("p")
            await pilot.pause()
            self.assertFalse([m for m in client.sent if m["cmd"] == "protect"])

    async def test_p_without_selection_notifies(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        notes = _record_notifications(app)
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": []})
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertFalse([m for m in client.sent if m["cmd"] == "protect"])
            self.assertIn("no process selected", [m for m, _ in notes])

    async def test_prompt_keeps_id_when_snapshot_changes(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot", "processes": [PROC1]})
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            # snapshot churn while the dialog is open must not retarget the id
            await client.messages.put({"msg": "snapshot", "processes": [PROC2]})
            await pilot.pause()
            await pilot.press("3", "enter")
            await pilot.pause()
            self.assertIn({"cmd": "protect", "id": "abc123", "limit": 3,
                           "gap": 5.0}, client.sent)

    async def test_prompt_title_shows_bracket_names_as_plain_text(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        bracket = {**PROC1, "name": "vim [x]"}
        async with app.run_test() as pilot:
            await client.messages.put({"msg": "snapshot",
                                       "processes": [bracket]})
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(type(app.screen).__name__, "ProtectScreen")
            title = str(app.screen.query(Static).first().render())
        self.assertIn("vim [x]", title)

    async def test_help_bar_lists_p_protect(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        async with app.run_test():
            bar = str(app.query_one("#help-bar", Static).render())
        self.assertIn("p protect", bar)


if __name__ == "__main__":
    unittest.main()
