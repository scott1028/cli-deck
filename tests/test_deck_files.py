"""N1/N2: stale deck files are swept on startup, and -l lists live decks."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cli_deck.deckd import (deck_paths, format_age, list_decks,
                            sweep_stale_decks)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class StaleDeckSweepTest(unittest.TestCase):
    def test_sweep_removes_dead_deck_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            rdir = Path(tmp)
            (rdir / "dead.json").write_text(json.dumps(
                {"socket": str(rdir / "dead.sock"), "pid": _dead_pid(),
                 "started_at": time.time(), "name": "dead"}))
            (rdir / "dead.sock").write_text("")
            (rdir / "live.json").write_text(json.dumps(
                {"socket": str(rdir / "live.sock"), "pid": os.getpid(),
                 "started_at": time.time(), "name": "live"}))
            (rdir / "live.sock").write_text("")

            removed = sweep_stale_decks(rdir)

            self.assertEqual([p.name for p in removed], ["dead.json"])
            self.assertFalse((rdir / "dead.json").exists())
            self.assertFalse((rdir / "dead.sock").exists())
            self.assertTrue((rdir / "live.json").exists())
            self.assertTrue((rdir / "live.sock").exists())

    def test_sweep_leaves_unreadable_meta_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            rdir = Path(tmp)
            (rdir / "junk.json").write_text("not json")
            self.assertEqual(sweep_stale_decks(rdir), [])
            self.assertTrue((rdir / "junk.json").exists())

    def test_sweep_on_missing_runtime_dir(self):
        self.assertEqual(sweep_stale_decks(Path(tempfile.mkdtemp()) / "gone"), [])

    def test_daemon_startup_sweeps_stale_entries(self):
        """A daemon that died without its stop path leaves .json/.sock behind;
        the next daemon startup must clear them."""
        stale_name = f"sweep-stale-{os.getpid()}"
        live_name = f"sweep-live-{os.getpid()}"
        stale_sock, stale_meta = deck_paths(stale_name)
        stale_meta.write_text(json.dumps(
            {"socket": str(stale_sock), "pid": _dead_pid(),
             "started_at": time.time(), "name": stale_name}))
        stale_sock.write_text("")  # leftover socket file from the dead daemon
        live_sock, _ = deck_paths(live_name)
        env = {**os.environ, "CLI_DECK_NAME": live_name}
        proc = subprocess.Popen([sys.executable, "-m", "cli_deck", "--daemon"],
                                env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            self._wait_for_socket(live_sock)
            self.assertFalse(stale_meta.exists(), "stale meta survived startup")
            self.assertFalse(stale_sock.exists(), "stale socket survived startup")
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
            for path in (stale_sock, stale_meta):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

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


class ListDecksTest(unittest.TestCase):
    def test_list_decks_skips_dead_pids(self):
        with tempfile.TemporaryDirectory() as tmp:
            rdir = Path(tmp)
            (rdir / "dead.json").write_text(json.dumps(
                {"socket": str(rdir / "dead.sock"), "pid": _dead_pid(),
                 "started_at": time.time(), "name": "dead"}))
            (rdir / "live.json").write_text(json.dumps(
                {"socket": str(rdir / "live.sock"), "pid": os.getpid(),
                 "started_at": time.time(), "name": "live"}))
            decks = list_decks(rdir)
            self.assertEqual([d["name"] for d in decks], ["live"])
            self.assertEqual(decks[0]["pid"], os.getpid())

    def test_format_age(self):
        self.assertEqual(format_age(3), "3s")
        self.assertEqual(format_age(75), "1m15s")
        self.assertEqual(format_age(3725), "1h02m")

    def test_dash_l_lists_live_decks(self):
        name = f"list-{os.getpid()}"
        sock, meta = deck_paths(name)
        env = {**os.environ, "CLI_DECK_NAME": name}
        proc = subprocess.Popen([sys.executable, "-m", "cli_deck", "--daemon"],
                                env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            StaleDeckSweepTest._wait_for_socket(sock)
            listing = subprocess.run([sys.executable, "-m", "cli_deck", "-l"],
                                     env=env, capture_output=True, text=True)
            self.assertEqual(listing.returncode, 0)
            self.assertIn(name, listing.stdout)
            self.assertIn(str(proc.pid), listing.stdout)
            self.assertIn(str(sock), listing.stdout)
            self.assertIn("age=", listing.stdout)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
            for path in (sock, meta):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
