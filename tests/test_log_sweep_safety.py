"""Sweep safety in log growth control.

A log whose stem is a registry id belongs to a row still on the dashboard
(live or exited) and must never be swept. With CLI_DECK_LOG_DIR the dir may
be shared with other decks or hold foreign files, so run_daemon turns the
dir sweep off; the per-file cap stays on.
"""
import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from cli_deck.deckd import DeckClient, DeckDaemon, deck_paths


class SweepSafetyTest(unittest.TestCase):
    @staticmethod
    def _seed(daemon: DeckDaemon, name: str, size: int, age: float) -> Path:
        daemon.logs.mkdir(parents=True, exist_ok=True)
        path = daemon.logs / f"{name}.log"
        path.write_bytes(b"z" * size)
        old = time.time() - age
        os.utime(path, (old, old))
        return path

    def test_sweep_keeps_log_of_listed_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            daemon = DeckDaemon(tmp_path / "deck.sock", tmp_path / "deck.json",
                                tmp_path / "logs", log_max_bytes=100,
                                log_total_factor=1, log_max_files=1)
            rec = daemon.registry.create("bash -i", ["bash", "-i"], "/tmp")
            listed = self._seed(daemon, rec.id, 300, 300)  # exited, still listed
            dead = self._seed(daemon, "deadbeef", 300, 200)

            daemon._sweep_logs()

            self.assertTrue(listed.exists(), "listed row's log must not be swept")
            self.assertFalse(dead.exists(), "unlisted dead log must still go")


class OverrideDirTest(unittest.TestCase):
    """End-to-end like tests/test_wrapper.py: a real `--daemon` process with
    CLI_DECK_LOG_DIR set must never sweep the (possibly shared) dir."""

    def test_override_dir_is_never_swept(self):
        name = f"swpguard{uuid.uuid4().hex[:8]}"
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            foreign = logs / "my-app.log"  # a file that is not ours
            foreign.write_bytes(b"m" * 300)
            old = time.time() - 300
            os.utime(foreign, (old, old))
            env = {**os.environ, "CLI_DECK_NAME": name,
                   "CLI_DECK_LOG_DIR": str(logs),
                   "CLI_DECK_LOG_MAX_BYTES": "100",
                   "CLI_DECK_LOG_MAX_FILES": "1"}
            proc = subprocess.Popen([sys.executable, "-m", "cli_deck", "--daemon"],
                                    env=env, start_new_session=True,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            sock, _ = deck_paths(name)
            try:
                self._wait_for_socket(sock)
                asyncio.run(self._session(sock, foreign))
            finally:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    self.fail("daemon did not stop")

    @staticmethod
    def _wait_for_socket(sock: Path) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(sock))
            except OSError:
                probe.close()
                time.sleep(0.05)
            else:
                probe.close()
                return
        raise TimeoutError("daemon socket never became connectable")

    @staticmethod
    async def _await_msg(client: DeckClient, kind: str) -> dict:
        while True:
            msg = await client.messages.get()
            if msg.get("msg") == kind:
                return msg

    async def _session(self, sock: Path, foreign: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(self._await_msg(client, "snapshot"), 5)
            await client.send({"cmd": "add", "command": "echo hi"})
            await asyncio.wait_for(self._await_msg(client, "added"), 10)
            # the spawn runs the sweep: the foreign log must survive it
            self.assertTrue(foreign.exists(),
                            "override dir must never be swept")
            await client.send({"cmd": "stop"})
            with contextlib.suppress(asyncio.TimeoutError):
                while True:  # drain before close: an RST would drop the stop
                    await asyncio.wait_for(client.messages.get(), 1)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
