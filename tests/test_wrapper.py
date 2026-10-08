"""M2: the `cli-deck <command...>` wrapper prefix, --no-launch and env overlay."""
import asyncio
import os
import pty
import select
import subprocess
import sys
import time
import unittest
from pathlib import Path

from cli_deck.__main__ import _split_wrapper
from cli_deck.deckd import DeckClient, deck_log_dir, deck_paths

MARK = "m2-wrapper-marker"


async def _deck_processes(sock: Path) -> list[dict]:
    client = await DeckClient.connect(sock)
    try:
        await client.send({"cmd": "list"})
        while True:
            msg = await client.messages.get()
            if msg.get("msg") == "snapshot":
                return msg["processes"]
    finally:
        await client.close()


async def _stop_daemon(sock: Path) -> None:
    try:
        client = await DeckClient.connect(sock)
    except OSError:
        return
    try:
        await client.send({"cmd": "stop"})
    finally:
        await client.close()
    deadline = time.monotonic() + 10
    while sock.exists() and time.monotonic() < deadline:
        time.sleep(0.05)


class WrapperPrefixTest(unittest.TestCase):
    """Drives the real `python -m cli_deck` with its stdio on a pty."""

    def test_split_wrapper_rules(self):
        # options stay options; the first bare token starts the command
        self.assertEqual(_split_wrapper(["--resume"]), (["--resume"], []))
        self.assertEqual(_split_wrapper(["ssh", "prod"]), ([], ["ssh", "prod"]))
        self.assertEqual(_split_wrapper(["--no-launch", "vim", "f"]),
                         (["--no-launch"], ["vim", "f"]))
        # `--` disambiguates a command that must start with a flag
        self.assertEqual(_split_wrapper(["--", "-L", "80:localhost:80"]),
                         ([], ["-L", "80:localhost:80"]))
        self.assertEqual(_split_wrapper(["--resume", "--", "-l"]),
                         (["--resume"], ["-l"]))

    def test_prefix_registers_and_attaches(self):
        name = "m2-attach"
        sock, _ = deck_paths(name)
        try:
            rc, out = self._run_attached(name, ["echo", MARK])
            self.assertEqual(rc, 0)
            self.assertIn(MARK.encode(), out)  # child output reached the terminal
            procs = asyncio.run(_deck_processes(sock))
            self.assertEqual([p["name"] for p in procs], [f"echo {MARK}"])
            self.assertEqual(procs[0]["state"], "exited")
        finally:
            self._teardown(name)

    def test_process_survives_wrapper_exit(self):
        name = "m2-survive"
        sock, _ = deck_paths(name)
        try:
            rc, out = self._run_attached(name, ["sleep", "300"], read_seconds=2.0)
            self.assertEqual(rc, 0)
            self.assertIn(b"detached", out)
            procs = asyncio.run(_deck_processes(sock))
            self.assertEqual([p["name"] for p in procs], ["sleep 300"])
            self.assertEqual(procs[0]["state"], "running")
        finally:
            self._teardown(name)

    def test_no_launch_prints_recipe(self):
        name = "m2-nolaunch"
        sock, _ = deck_paths(name)
        try:
            rc, out = self._run_plain(name, ["--no-launch", "echo", "hi"])
            self.assertEqual(rc, 0)
            for token in ("deck:", "socket:", "id:", "command:", "attach:"):
                self.assertIn(token, out)
            self.assertIn(f"deck:    {name}", out)
            proc_id = self._recipe_id(out)
            procs = asyncio.run(_deck_processes(sock))
            self.assertEqual([p["id"] for p in procs], [proc_id])
        finally:
            self._teardown(name)

    def test_env_overlay_reaches_child(self):
        name = "m2-env"
        try:
            _, out = self._run_plain(name, ["--no-launch", "echo FOO=$FOO"],
                                     env_extra={"FOO": "bar"})
            log = self._await_log_output(name, self._recipe_id(out), b"FOO=bar")
            self.assertIn(b"FOO=bar", log)
        finally:
            self._teardown(name)

    def test_exported_bash_function_visible_in_child(self):
        name = "m2-func"
        body = "() {  echo fn-visible\n}"
        try:
            _, out = self._run_plain(
                name, ["--no-launch", "declare -F m2fn && m2fn"],
                env_extra={"BASH_FUNC_m2fn%%": body})
            log = self._await_log_output(name, self._recipe_id(out), b"fn-visible")
            self.assertIn(b"m2fn", log)       # declare -F m2fn found it
            self.assertIn(b"fn-visible", log)  # and it is callable
        finally:
            self._teardown(name)

    # -- helpers ------------------------------------------------------------

    def _run_attached(self, name: str, args: list[str],
                      env_extra: dict | None = None,
                      read_seconds: float = 3.0) -> tuple[int, bytes]:
        """Run the wrapper with stdio on a pty, then detach with Ctrl-]."""
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, "-m", "cli_deck", *args],
            env=self._env(name, env_extra),
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        os.close(slave)
        out = b""
        deadline = time.monotonic() + read_seconds
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    out += os.read(master, 65536)
            os.write(master, b"\x1d")  # Ctrl-]
            try:
                rc = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail("wrapper did not exit after Ctrl-]")
            while True:
                ready, _, _ = select.select([master], [], [], 0.2)
                if not ready:
                    break
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                out += data
            return rc, out
        finally:
            os.close(master)

    def _run_plain(self, name: str, args: list[str],
                   env_extra: dict | None = None) -> tuple[int, str]:
        """Run the wrapper with pipes (used with --no-launch: no attach, no tty)."""
        proc = subprocess.run([sys.executable, "-m", "cli_deck", *args],
                              env=self._env(name, env_extra),
                              capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout

    def _env(self, name: str, env_extra: dict | None) -> dict[str, str]:
        return {**os.environ, "CLI_DECK_NAME": name, **(env_extra or {})}

    @staticmethod
    def _recipe_id(stdout: str) -> str:
        for line in stdout.splitlines():
            if line.startswith("id:"):
                return line.split(":", 1)[1].strip()
        raise AssertionError(f"no id in recipe output:\n{stdout}")

    @staticmethod
    def _await_log_output(name: str, proc_id: str, marker: bytes,
                          timeout: float = 20.0) -> bytes:
        log = deck_log_dir(name) / f"{proc_id}.log"
        deadline = time.monotonic() + timeout
        data = b""
        while time.monotonic() < deadline:
            try:
                data = log.read_bytes()
            except OSError:
                time.sleep(0.1)
                continue
            if marker in data:
                return data
            time.sleep(0.1)
        raise AssertionError(f"{marker!r} never appeared in {log}: {data!r}")

    def _teardown(self, name: str) -> None:
        sock, _ = deck_paths(name)
        asyncio.run(_stop_daemon(sock))
        logs = deck_log_dir(name)
        if logs.exists():
            for path in logs.iterdir():
                path.unlink(missing_ok=True)
            logs.rmdir()


if __name__ == "__main__":
    unittest.main()
