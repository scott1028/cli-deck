import asyncio
import base64
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cli_deck.deckd import DeckClient, DeckDaemon


async def _await_msg(client: DeckClient, kind: str) -> dict:
    while True:
        msg = await client.messages.get()
        if msg.get("msg") == kind:
            return msg


class ProtocolTest(unittest.TestCase):
    def _run_session(self, session, block_logs: bool = False) -> None:
        """Start a daemon on a temp socket, run an async client session."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sock = tmp_path / "deck.sock"
            logs = tmp_path / "logs"
            if block_logs:  # a file where the log dir must be: mkdir/open fail
                logs.write_text("blocker")
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
        # Poll with a real connect: the socket file appears at bind() time,
        # before listen(), so exists() alone races with ECONNREFUSED.
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
    async def _collect_log(client: DeckClient, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            msg = await _await_msg(client, "log")
            data += base64.b64decode(msg["data"])
        return data

    @staticmethod
    async def _await_exited(client: DeckClient) -> dict:
        while True:
            snap = await _await_msg(client, "snapshot")
            if all(p["state"] == "exited" for p in snap["processes"]):
                return snap

    # -- tests --------------------------------------------------------------

    def test_round_trip_on_temp_socket(self):
        self._run_session(self._round_trip_session)

    async def _round_trip_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            first = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(first["processes"], [])

            await client.send({"cmd": "add", "command": "sleep 0.5; echo live-log"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(len(snap["processes"]), 1)
            proc_id = snap["processes"][0]["id"]

            # attach subscribes this connection; later output streams as log
            await client.send({"cmd": "attach", "id": proc_id})
            attached = await asyncio.wait_for(_await_msg(client, "attached"), 5)
            self.assertEqual(attached["id"], proc_id)

            log_data = await asyncio.wait_for(self._collect_log(client, b"live-log"), 20)
            self.assertIn(b"live-log", log_data)

            snap = await asyncio.wait_for(self._await_exited(client), 20)
            self.assertEqual(snap["processes"][0]["exit_code"], 0)

            # re-attach replays the ring buffer
            await client.send({"cmd": "attach", "id": proc_id})
            attached = await asyncio.wait_for(_await_msg(client, "attached"), 5)
            self.assertIn(b"live-log", base64.b64decode(attached["replay"]))

            # winsize and attach_input on a dead process produce errors
            await client.send({"cmd": "winsize", "id": proc_id, "rows": 10, "cols": 40})
            err = await asyncio.wait_for(_await_msg(client, "error"), 5)
            self.assertIn("no live process", err["message"])

            await client.send({"cmd": "bogus"})
            err = await asyncio.wait_for(_await_msg(client, "error"), 5)
            self.assertIn("unknown cmd", err["message"])

            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_winsize_applies_to_live_process(self):
        self._run_session(self._winsize_session)

    async def _winsize_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)  # initial
            await client.send({"cmd": "add", "command": "sleep 0.5; stty size"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc_id = snap["processes"][0]["id"]
            await client.send({"cmd": "attach", "id": proc_id})
            await client.send({"cmd": "winsize", "id": proc_id,
                               "rows": 30, "cols": 100})
            data = await asyncio.wait_for(self._collect_log(client, b"30 100"), 20)
            self.assertIn(b"30 100", data)
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_add_failure_leaves_no_phantom_record(self):
        # P3: a broken log dir must not leave a "running" record without a handle
        self._run_session(self._phantom_session, block_logs=True)

    async def _phantom_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)  # initial
            await client.send({"cmd": "add", "command": "echo nope"})
            err = await asyncio.wait_for(_await_msg(client, "error"), 5)
            self.assertIn("add failed", err["message"])
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            self.assertEqual(snap["processes"], [])
            await client.send({"cmd": "stop"})
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
