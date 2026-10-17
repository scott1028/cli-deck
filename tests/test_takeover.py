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


class TakeoverPumpTest(unittest.TestCase):
    """Headless proof of the takeover byte pump: an operator pty pair stands
    in for the real terminal, `cat` stands in for a full-screen program."""

    def test_takeover_round_trip_detach_and_survival(self):
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
            await client.send({"cmd": "add", "argv": ["bash", "-ic", "exec cat"]})
            snap = await asyncio.wait_for(self._await_snapshot(client), 5)
            proc_id = snap["processes"][0]["id"]

            pump = threading.Thread(target=run_takeover,
                                    args=(sock, proc_id, op_slave, op_slave),
                                    daemon=True)
            pump.start()
            await asyncio.sleep(0.5)

            # keystrokes typed on the operator terminal reach the process...
            os.write(op_master, b"hello-takeover\r")
            # ...and its output comes back as full-screen bytes
            data = b""
            deadline = time.monotonic() + 15
            while b"hello-takeover" not in data and time.monotonic() < deadline:
                readable, _, _ = select.select([op_master], [], [], 0.5)
                if readable:
                    data += os.read(op_master, 65536)
            self.assertIn(b"hello-takeover", data)

            # Ctrl-] detaches the pump
            os.write(op_master, b"\x1d")
            pump.join(timeout=5)
            self.assertFalse(pump.is_alive(), "pump did not exit on Ctrl-]")

            # the process kept running the whole time
            await client.send({"cmd": "list"})
            snap = await asyncio.wait_for(self._await_snapshot(client), 5)
            self.assertEqual(snap["processes"][0]["state"], "running")

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
