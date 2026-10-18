"""Takeover keeps the cursor position on detach.

DECSTBM (ESC[r) homes the cursor. On the primary screen a plain line-output
child leaves the pump's reset there, so after Ctrl-] and `d` the shell prompt
draws at the top. The reset must be wrapped in DECSC/DECRC (ESC 7 / ESC 8) to
keep the cursor position.
"""
import asyncio
import os
import pty
import select
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cli_deck.deckd import DeckClient, DeckDaemon, run_takeover

RESET_WITH_CURSOR = b"\x1b7\x1b[r\x1b8"


class TakeoverCursorTest(unittest.TestCase):
    """Mirrors tests/test_takeover_scroll_region.py: daemon in a thread,
    an operator pty pair stands in for the real terminal, pump on a thread."""

    def test_detach_reset_preserves_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sock = tmp_path / "deck.sock"
            daemon = DeckDaemon(sock, tmp_path / "deck.json", tmp_path / "logs")
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=lambda: (asyncio.set_event_loop(loop),
                                loop.run_until_complete(daemon.run())),
                daemon=True)
            thread.start()
            try:
                self._wait_for_socket(sock)
                asyncio.run(self._session(sock))
            finally:
                thread.join(timeout=15)
                if loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)
                    thread.join(timeout=5)

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

    async def _session(self, sock: Path) -> None:
        client = await DeckClient.connect(sock)
        op_master, op_slave = pty.openpty()
        try:
            # plain line-output child, like the reported npm case
            await client.send({"cmd": "add",
                               "command": "echo ready; sleep 30"})
            snap = await asyncio.wait_for(self._await_snapshot(client), 5)
            proc_id = snap["processes"][0]["id"]

            pump = threading.Thread(target=run_takeover,
                                    args=(sock, proc_id, op_slave, op_slave),
                                    daemon=True)
            pump.start()

            data = b""
            deadline = time.monotonic() + 15
            while b"ready" not in data and time.monotonic() < deadline:
                readable, _, _ = select.select([op_master], [], [], 0.5)
                if readable:
                    data += os.read(op_master, 65536)
            self.assertIn(b"ready", data)

            # Ctrl-] detaches the pump
            os.write(op_master, b"\x1d")
            pump.join(timeout=5)
            self.assertFalse(pump.is_alive(), "pump did not exit on Ctrl-]")

            # drain whatever the pump still wrote on detach
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                readable, _, _ = select.select([op_master], [], [], 0.2)
                if not readable:
                    break
                data += os.read(op_master, 65536)

            # the scroll-region reset must save and restore the cursor
            # (DECSC/DECRC), or DECSTBM homes it on the primary screen
            self.assertIn(RESET_WITH_CURSOR, data)

            await client.send({"cmd": "kill", "id": proc_id})
            await client.send({"cmd": "stop"})
        finally:
            await client.close()
            os.close(op_master)
            os.close(op_slave)

    @staticmethod
    async def _await_snapshot(client: DeckClient) -> dict:
        while True:
            msg = await client.messages.get()
            if msg.get("msg") == "snapshot" and msg["processes"]:
                return msg


if __name__ == "__main__":
    unittest.main()
