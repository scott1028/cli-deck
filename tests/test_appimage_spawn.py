"""ensure_daemon inside the AppImage, where sys.executable is the AppImage itself."""
import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cli_deck.deckd import deck_paths, ensure_daemon


def _write_fake_appimage(tmp: str) -> str:
    # Like AppRun: every launch is `python -m cli_deck <args>`.
    path = Path(tmp) / "cli-deck.AppImage"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m cli_deck "$@"\n')
    path.chmod(0o755)
    return str(path)


def _stop_daemon(meta_path: Path) -> None:
    try:
        meta = meta_path.read_text()
    except FileNotFoundError:
        return
    pid = json.loads(meta)["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)  # the spawned daemon is this process's child
    except (ProcessLookupError, ChildProcessError):
        pass


class AppImageDaemonSpawnTest(unittest.TestCase):
    def test_daemon_comes_up_when_sys_executable_is_the_appimage(self):
        name = f"appimage-spawn-{os.getpid()}"
        sock, meta_path = deck_paths(name)
        self.addCleanup(_stop_daemon, meta_path)
        with tempfile.TemporaryDirectory() as tmp:
            appimage = _write_fake_appimage(tmp)
            env = {"CLI_DECK_NAME": name, "APPIMAGE": appimage,
                   "APPDIR": sys.prefix}
            # python-appimage sets sys.executable to the AppImage path
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(sys, "executable", appimage):
                try:
                    meta = asyncio.run(ensure_daemon(sock, meta_path))
                except RuntimeError as exc:
                    self.fail(f"daemon did not start: {exc}")
            self.assertEqual(meta["socket"], str(sock))

    def test_foreign_appimage_env_is_ignored(self):
        # e.g. a terminal emulator AppImage leaks APPIMAGE/APPDIR into the shell
        name = f"foreign-appimage-{os.getpid()}"
        sock, meta_path = deck_paths(name)
        self.addCleanup(_stop_daemon, meta_path)
        with tempfile.TemporaryDirectory() as tmp:
            foreign = Path(tmp) / "terminal.AppImage"
            foreign.write_text("#!/bin/sh\nexit 3\n")
            foreign.chmod(0o755)
            env = {"CLI_DECK_NAME": name, "APPIMAGE": str(foreign),
                   "APPDIR": tmp}
            with mock.patch.dict(os.environ, env):
                try:
                    meta = asyncio.run(ensure_daemon(sock, meta_path))
                except RuntimeError as exc:
                    self.fail(f"daemon did not start: {exc}")
            self.assertEqual(meta["socket"], str(sock))


if __name__ == "__main__":
    unittest.main()
