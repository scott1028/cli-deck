"""Takeover resets the child's scroll region on detach.

A child may leave a DECSTBM scroll region (e.g. watch's ESC[1;24r); if it
survives the takeover, Textual's line-by-line repaint ("\n") scrolls rows and
the dashboard loses its header. The pump must reset the region on detach.
"""
import asyncio
import os
import pty
import re
import select
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cli_deck.deckd import DeckClient, DeckDaemon, run_takeover

DECSTBM = re.compile(rb"\x1b\[[0-9;]*r")


class TakeoverScrollRegionTest(unittest.TestCase):
    """Mirrors tests/test_takeover.py: daemon in a thread, an operator pty
    pair stands in for the real terminal, pump runs on its own thread."""

    def test_detach_resets_child_scroll_region(self):
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
            # the child sets a scroll region, like watch does
            await client.send({"cmd": "add",
                               "command": "printf '\\033[1;5r'; echo ready; "
                                          "sleep 30"})
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

            # the last DECSTBM the operator terminal sees must be a full
            # screen reset, not the child's leftover region
            matches = DECSTBM.findall(data)
            self.assertTrue(matches, "no DECSTBM in operator output")
            self.assertEqual(matches[-1], b"\x1b[r")

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
