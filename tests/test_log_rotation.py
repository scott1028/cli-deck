"""Builtin log growth control.

CLI_DECK_LOG_MAX_BYTES caps each raw log in place (rewrite to its last
half-budget, same inode); a truncate also sweeps the deck log dir, and so
does every spawn: oldest non-live logs go until the dir is inside
factor * max_bytes total bytes and max_files count. Live logs are never
candidates. max_bytes=0 turns the builtin off for OS logrotate.
"""
import asyncio
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cli_deck.deckd import (LOG_MAX_BYTES_DEFAULT, LOG_MAX_FILES_DEFAULT,
                            LOG_TOTAL_FACTOR_DEFAULT, DeckClient, DeckDaemon,
                            log_max_bytes, log_max_files, log_total_factor)


def _daemon(tmp_path: Path, max_bytes: int = 0, factor: int = 0,
            files: int = 0) -> DeckDaemon:
    """Unstarted daemon: the sync helpers under test need no loop."""
    return DeckDaemon(tmp_path / "deck.sock", tmp_path / "deck.json",
                      tmp_path / "logs", log_max_bytes=max_bytes,
                      log_total_factor=factor, log_max_files=files)


class EnvKnobTest(unittest.TestCase):
    def test_defaults_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(log_max_bytes(), LOG_MAX_BYTES_DEFAULT)
            self.assertEqual(log_total_factor(), LOG_TOTAL_FACTOR_DEFAULT)
            self.assertEqual(log_max_files(), LOG_MAX_FILES_DEFAULT)

    def test_zero_disables(self):
        with patch.dict(os.environ, {"CLI_DECK_LOG_MAX_BYTES": "0",
                                     "CLI_DECK_LOG_MAX_TOTAL_FACTOR": "0",
                                     "CLI_DECK_LOG_MAX_FILES": "0"}):
            self.assertEqual(log_max_bytes(), 0)
            self.assertEqual(log_total_factor(), 0)
            self.assertEqual(log_max_files(), 0)

    def test_valid_values(self):
        with patch.dict(os.environ, {"CLI_DECK_LOG_MAX_BYTES": "1024",
                                     "CLI_DECK_LOG_MAX_TOTAL_FACTOR": "3",
                                     "CLI_DECK_LOG_MAX_FILES": "7"}):
            self.assertEqual(log_max_bytes(), 1024)
            self.assertEqual(log_total_factor(), 3)
            self.assertEqual(log_max_files(), 7)

    def test_garbage_and_negative_fall_back(self):
        with patch.dict(os.environ, {"CLI_DECK_LOG_MAX_BYTES": "10 MB",
                                     "CLI_DECK_LOG_MAX_TOTAL_FACTOR": "-1",
                                     "CLI_DECK_LOG_MAX_FILES": ""}):
            self.assertEqual(log_max_bytes(), LOG_MAX_BYTES_DEFAULT)
            self.assertEqual(log_total_factor(), LOG_TOTAL_FACTOR_DEFAULT)
            self.assertEqual(log_max_files(), LOG_MAX_FILES_DEFAULT)


class TruncateLogTest(unittest.TestCase):
    def test_cap_rewrites_last_half_budget_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp), max_bytes=2000)
            daemon.logs.mkdir(parents=True)
            log_path = daemon.logs / "abc12345.log"
            fh = open(log_path, "w+b")  # same mode the daemon opens logs with
            fh.write(b"HEAD" + b"x" * 5000 + b"TAIL")
            fh.flush()

            new_size = daemon._truncate_log(fh)

            self.assertEqual(new_size, 1000)  # keep = max_bytes // 2
            data = log_path.read_bytes()
            self.assertEqual(len(data), 1000)
            self.assertNotIn(b"HEAD", data)  # head cut away
            self.assertTrue(data.endswith(b"TAIL"))  # tail kept
            # same fd stays usable at the new end: appends continue the file
            fh.write(b"MORE")
            fh.flush()
            self.assertEqual(log_path.read_bytes()[-4:], b"MORE")
            fh.close()

    def test_file_shrunk_under_us_only_resyncs(self):
        # operator/logrotate copytruncate cut it: nothing to do, report size
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp), max_bytes=2000)
            daemon.logs.mkdir(parents=True)
            log_path = daemon.logs / "abc12345.log"
            log_path.write_bytes(b"y" * 500)
            fh = open(log_path, "r+b")

            self.assertEqual(daemon._truncate_log(fh), 500)
            self.assertEqual(log_path.read_bytes(), b"y" * 500)
            fh.close()


class SweepLogsTest(unittest.TestCase):
    @staticmethod
    def _seed(daemon: DeckDaemon, name: str, size: int, age: float) -> Path:
        daemon.logs.mkdir(parents=True, exist_ok=True)
        path = daemon.logs / f"{name}.log"
        path.write_bytes(b"z" * size)
        old = time.time() - age
        os.utime(path, (old, old))
        return path

    def test_oldest_dead_logs_go_until_byte_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp), max_bytes=1000, factor=2)  # budget 2000
            a = self._seed(daemon, "aaaa1111", 900, 300)
            b = self._seed(daemon, "bbbb2222", 900, 200)
            c = self._seed(daemon, "cccc3333", 900, 100)
            live = self._seed(daemon, "dddd4444", 900, 400)  # oldest, but live
            daemon.log_files["dddd4444"] = object()

            daemon._sweep_logs()

            self.assertFalse(a.exists())  # oldest dead first
            self.assertFalse(b.exists())  # 3600 -> 2700 -> 1800 <= 2000
            self.assertTrue(c.exists())
            self.assertTrue(live.exists())  # live never a candidate

    def test_live_only_dir_may_stay_over_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp), max_bytes=1000, factor=1)  # budget 1000
            live = self._seed(daemon, "dddd4444", 1500, 10)
            daemon.log_files["dddd4444"] = object()

            daemon._sweep_logs()

            self.assertTrue(live.exists())

    def test_count_cap_drops_oldest_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp), max_bytes=1000, files=3)
            seeded = [self._seed(daemon, f"dead{i:04d}", 10, 100 - i)
                      for i in range(5)]
            gone, kept = seeded[:3], seeded[3:]  # 3 oldest of 6 files must go
            live = self._seed(daemon, "dddd4444", 10, 1)
            daemon.log_files["dddd4444"] = object()

            daemon._sweep_logs()

            self.assertTrue(all(not p.exists() for p in gone))
            self.assertTrue(all(p.exists() for p in kept))
            self.assertTrue(live.exists())
            self.assertEqual(len(list(daemon.logs.glob("*.log"))), 3)

    def test_max_bytes_zero_sweeps_nothing(self):
        # builtin fully off: logrotate owns the dir, even with other knobs on
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp), max_bytes=0, factor=10, files=1)
            seeded = [self._seed(daemon, f"dead{i:04d}", 10, 100 - i)
                      for i in range(4)]

            daemon._sweep_logs()

            self.assertTrue(all(p.exists() for p in seeded))

    def test_both_conditions_off_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp), max_bytes=1000)  # factor=0, files=0
            seeded = [self._seed(daemon, f"dead{i:04d}", 900, 100 - i)
                      for i in range(4)]

            daemon._sweep_logs()

            self.assertTrue(all(p.exists() for p in seeded))


async def _await_msg(client: DeckClient, kind: str) -> dict:
    while True:
        msg = await client.messages.get()
        if msg.get("msg") == kind:
            return msg


class DaemonLogCapTest(unittest.TestCase):
    """End-to-end: the pump truncates real pty output; spawn sweeps the dir."""

    def _run_session(self, session, **knobs) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sock = tmp_path / "deck.sock"
            daemon = DeckDaemon(sock, tmp_path / "deck.json",
                                tmp_path / "logs", **knobs)
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
    async def _add_and_await_exited(client: DeckClient, command: str) -> dict:
        await client.send({"cmd": "add", "command": command, "name": "chatty"})
        while True:
            snap = await asyncio.wait_for(_await_msg(client, "snapshot"), 20)
            if snap["processes"] and all(p["state"] == "exited"
                                         for p in snap["processes"]):
                return snap["processes"][0]

    # -- tests --------------------------------------------------------------

    def test_chatty_process_log_is_capped_and_keeps_tail(self):
        self._run_session(self._capped_session, log_max_bytes=4096)

    async def _capped_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc = await self._add_and_await_exited(
                client, "printf 'HEADMARKER\\n'; "
                        "yes fillerline | head -c 200000; "
                        "printf 'TAILMARKER\\n'")
            data = Path(proc["log_path"]).read_bytes()
            # one chunk (65536) of overshoot is possible before the cap bites
            self.assertLessEqual(len(data), 4096 + 65536)
            self.assertIn(b"TAILMARKER", data)  # tail kept
            self.assertNotIn(b"HEADMARKER", data)  # head cut away
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_max_bytes_zero_keeps_the_whole_log(self):
        self._run_session(self._uncapped_session, log_max_bytes=0)

    async def _uncapped_session(self, sock: Path, tmp: Path) -> None:
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc = await self._add_and_await_exited(
                client, "printf 'HEADMARKER\\n'; "
                        "yes fillerline | head -c 200000; "
                        "printf 'TAILMARKER\\n'")
            data = Path(proc["log_path"]).read_bytes()
            self.assertGreater(len(data), 100000)  # builtin off: nothing cut
            self.assertIn(b"HEADMARKER", data)
            await client.send({"cmd": "stop"})
        finally:
            await client.close()

    def test_spawn_sweeps_old_dead_logs_to_the_byte_budget(self):
        self._run_session(self._spawn_sweep_session,
                          log_max_bytes=1000, log_total_factor=1)  # budget 1000

    async def _spawn_sweep_session(self, sock: Path, tmp: Path) -> None:
        logs = tmp / "logs"
        logs.mkdir(parents=True)
        seeded = []
        for i, age in enumerate((300, 200, 100)):  # 3 x 900B dead, budget 1000
            path = logs / f"dead{i:04d}.log"
            path.write_bytes(b"z" * 900)
            os.utime(path, (time.time() - age,) * 2)
            seeded.append(path)
        client = await DeckClient.connect(sock)
        try:
            await asyncio.wait_for(_await_msg(client, "snapshot"), 5)
            proc = await self._add_and_await_exited(client, "echo hi")
            self.assertTrue(Path(proc["log_path"]).exists())
            # 2700 -> 1800 -> 900: the two oldest dead logs must go
            self.assertFalse(seeded[0].exists(), "oldest dead log must go")
            self.assertFalse(seeded[1].exists(), "second oldest dead log must go")
            self.assertTrue(seeded[2].exists(), "budget met: newest dead stays")
            await client.send({"cmd": "stop"})
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
