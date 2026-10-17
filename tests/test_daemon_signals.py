import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cli_deck.deckd import deck_paths


class DaemonSignalCleanupTest(unittest.TestCase):
    """A daemon killed by SIGTERM/SIGINT must run its stop path:
    no stale socket or metadata left in the runtime dir."""

    def test_sigterm_cleanup(self):
        self._check(signal.SIGTERM)

    def test_sigint_cleanup(self):
        self._check(signal.SIGINT)

    def _check(self, sig) -> None:
        name = f"pytest-{sig.name.lower()}"
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "CLI_DECK_NAME": name,
                   "CLI_DECK_LOG_DIR": str(Path(tmp) / "logs")}
            proc = subprocess.Popen(
                [sys.executable, "-m", "cli_deck", "--daemon"],
                cwd=tmp, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                sock, meta = deck_paths(name)
                deadline = time.monotonic() + 10
                connected = False
                while time.monotonic() < deadline:
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        probe.connect(str(sock))
                    except OSError:
                        time.sleep(0.05)
                    else:
                        probe.close()
                        connected = True
                        break
                self.assertTrue(connected, "daemon socket never became connectable")

                proc.send_signal(sig)
                proc.wait(timeout=10)

                deadline = time.monotonic() + 5
                while sock.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(sock.exists(), "stale socket left behind")
                self.assertFalse(meta.exists(), "stale meta left behind")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    unittest.main()
