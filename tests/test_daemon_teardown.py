"""P4: the daemon process itself must leave after stop/SIGINT/SIGTERM.

Runs the daemon as a real subprocess with a long-lived `sleep 300` child, then
asserts the pid disappears from /proc within 5s, socket + meta are gone, and no
`sleep 300` orphan is left behind.
"""
import asyncio
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cli_deck.deckd import DeckClient, deck_paths

EXIT_TIMEOUT = 5.0


def _deck_children(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return [int(x) for x in out.stdout.split()]


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


async def _await_snapshot(client: DeckClient, min_processes: int = 0) -> dict:
    while True:
        msg = await client.messages.get()
        if msg.get("msg") == "snapshot" and len(msg.get("processes", [])) >= min_processes:
            return msg


class DaemonTeardownTest(unittest.TestCase):
    STUBBORN = ["bash", "-ic", "trap '' INT TERM; sleep 300"]

    def test_stop_exits_bounded(self):
        self._check("stop")

    def test_sigterm_exits_bounded(self):
        self._check(signal.SIGTERM)

    def test_sigint_exits_bounded(self):
        self._check(signal.SIGINT)

    def test_stop_with_kill_in_flight_exits_bounded(self):
        # The kill escalation thread is still parked when `stop` lands: the
        # daemon must not end up joined to it forever.
        self._check("stop", kill_first=True)

    def test_many_stubborn_children_exit_bounded(self):
        # Children that ignore SIGINT and SIGKILL only arrive after the full
        # escalation, so killing them one after another would blow the budget.
        self._check("stop", argvs=[self.STUBBORN] * 4)

    def _check(self, trigger, kill_first: bool = False,
               argvs: list[list[str]] = None) -> None:
        argvs = argvs if argvs is not None else [["sleep", "300"]]
        label = trigger if isinstance(trigger, str) else trigger.name
        name = f"teardown-{label}-{int(kill_first)}-{len(argvs)}"
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "CLI_DECK_NAME": name,
                   "CLI_DECK_LOG_DIR": str(Path(tmp) / "logs")}
            proc = subprocess.Popen(
                [sys.executable, "-m", "cli_deck", "--daemon"],
                cwd=tmp, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
            sock, meta = deck_paths(name)
            kids: list[int] = []
            try:
                self._wait_for_socket(sock)
                kids = asyncio.run(self._session(sock, proc.pid, trigger,
                                                 kill_first, argvs))
                if not isinstance(trigger, str):
                    proc.send_signal(trigger)

                started = time.monotonic()
                try:
                    code = proc.wait(timeout=EXIT_TIMEOUT)
                except subprocess.TimeoutExpired:
                    self.fail(f"daemon pid {proc.pid} still alive "
                              f"{EXIT_TIMEOUT}s after {label} "
                              f"(/proc entry: {Path(f'/proc/{proc.pid}').exists()})")
                elapsed = time.monotonic() - started

                self.assertLess(elapsed, EXIT_TIMEOUT)
                self.assertFalse(Path(f"/proc/{proc.pid}").exists(),
                                 "daemon pid still present in /proc")
                self.assertFalse(sock.exists(), "stale socket left behind")
                self.assertFalse(meta.exists(), "stale meta left behind")
                for kid in kids:
                    self.assertFalse(Path(f"/proc/{kid}").exists(),
                                     f"orphaned deck process {kid} ({_cmdline(kid)})")
                print(f"\n  {label}: daemon pid {proc.pid} gone from /proc after "
                      f"{elapsed:.2f}s (rc={code}), deck children {kids} reaped")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                for kid in kids:
                    try:
                        os.kill(kid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    async def _session(self, sock: Path, daemon_pid: int, trigger,
                       kill_first: bool, argvs: list[list[str]]) -> list[int]:
        """Add the deck processes, report their pids, then stop the daemon."""
        client = await DeckClient.connect(sock)
        try:
            await _await_snapshot(client)
            for argv in argvs:
                await client.send({"cmd": "add", "argv": argv})
            snap = await _await_snapshot(client, min_processes=len(argvs))
            proc_ids = [p["id"] for p in snap["processes"]]
            self.assertEqual(len(proc_ids), len(argvs))

            kids = await self._wait_for_deck_children(daemon_pid, len(argvs))
            self.assertTrue(kids, "deck child never started")
            self.assertTrue(any("sleep 300" in _cmdline(kid) for kid in kids),
                            f"no `sleep 300` among {kids}: {[_cmdline(k) for k in kids]}")

            if kill_first:
                for proc_id in proc_ids:
                    await client.send({"cmd": "kill", "id": proc_id})
            if isinstance(trigger, str):
                await client.send({"cmd": "stop"})
            return kids
        finally:
            # DeckClient drains before closing: closing with unread broadcast
            # data makes the kernel send RST, which drops the queued `stop`.
            await client.close()

    @staticmethod
    async def _wait_for_deck_children(daemon_pid: int, want: int) -> list[int]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            kids = _deck_children(daemon_pid)
            if len(kids) >= want:
                return kids
            await asyncio.sleep(0.05)
        return _deck_children(daemon_pid)

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


if __name__ == "__main__":
    unittest.main()
