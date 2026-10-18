"""The remove notice.

x on an exited row removes it without a confirm; the daemon answers the
requesting client with a notice naming the kept log file, and the TUI shows
it as a bottom-right toast (markup=False so bracketed names stay plain).
"""
import asyncio
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cli_deck.app import DeckApp
from cli_deck.deckd import DeckClient, DeckDaemon


async def _await_msg(client: DeckClient, kind: str) -> dict:
    while True:
        msg = await client.messages.get()
        if msg.get("msg") == kind:
            return msg


class DaemonRemoveNoticeTest(unittest.TestCase):
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

    @staticmethod
    async def _add_and_await_exited(client: DeckClient) -> dict:
        """Add a short-lived child and return its exited snapshot entry."""
        await client.send({"cmd": "add", "command": "echo done", "name": "job"})
        while True:
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 20)
            if snap["processes"] and all(p["state"] == "exited"
                                         for p in snap["processes"]):
                return snap["processes"][0]

    @staticmethod
    async def _poll_notice(client: DeckClient, timeout: float = 5.0) -> dict | None:
        """Next notice message within `timeout`, else None."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = await asyncio.wait_for(client.messages.get(), remaining)
            except asyncio.TimeoutError:
                return None
            if msg.get("msg") == "notice":
                return msg

    # -- tests --------------------------------------------------------------

    def test_remove_exited_notice_omits_log_when_file_gone(self):
        self._run_session(self._notice_without_log_session)

    async def _notice_without_log_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc = await self._add_and_await_exited(client)
            Path(proc["log_path"]).unlink()  # operator deleted the log

            await client.send({"cmd": "remove", "id": proc["id"],
                               "terminate_first": False})
            notice = await self._poll_notice(client)
            self.assertIsNotNone(notice, "expected a notice after remove")
            self.assertIn("removed 'job'", notice["message"])
            self.assertNotIn("log kept", notice["message"])
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_remove_exited_notices_with_kept_log_path(self):
        self._run_session(self._notice_with_log_session)

    async def _notice_with_log_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc = await self._add_and_await_exited(client)

            await client.send({"cmd": "remove", "id": proc["id"],
                               "terminate_first": False})
            notice = await self._poll_notice(client)
            self.assertIsNotNone(notice, "expected a notice after remove")
            self.assertIn("removed 'job'", notice["message"])
            self.assertIn("log kept:", notice["message"])
            self.assertIn(proc["log_path"], notice["message"])

            # the row is gone from the registry
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(snap["processes"], [])
            await client.send({"cmd": "stop"})
        finally:
            await client.close()


class FakeClient:
    """Stand-in for DeckClient: records sends, feeds canned messages."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.messages: asyncio.Queue = asyncio.Queue()

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)


class TuiNoticeToastTest(unittest.IsolatedAsyncioTestCase):
    async def test_notice_toast_keeps_bracketed_names_as_plain_text(self):
        client = FakeClient()
        app = DeckApp(client, Path("/nonexistent.sock"))
        notified: list[tuple[str, dict]] = []

        def fake_notify(message: str, **kwargs) -> None:
            notified.append((message, kwargs))

        app.notify = fake_notify  # capture the toast call (markup kwarg)
        async with app.run_test() as pilot:
            await client.messages.put(
                {"msg": "notice",
                 "message": "removed 'python [bold]demo[/bold]'; "
                            "log kept: /tmp/x/abc123.log"})
            await pilot.pause()

        self.assertEqual(len(notified), 1)
        message, kwargs = notified[0]
        self.assertIn("[bold]demo[/bold]", message)
        self.assertIs(kwargs.get("markup"), False)


if __name__ == "__main__":
    unittest.main()
