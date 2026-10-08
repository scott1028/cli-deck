"""Headless M2 verification: `cli-deck vim` under a real operator pty.

Flow: cli-deck vim -> vim takes over the pty -> type text, ESC reaches vim ->
Ctrl-] detaches and the wrapper exits -> vim keeps running -> cli-deck -l ->
TUI table -> --resume takeover replays the vim screen with the typed text.
"""
import asyncio
import os
import pty
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

from cli_deck import deckd
from cli_deck.app import DeckApp
from textual.widgets import DataTable

MARK = "m2-vim-demo-42"
NAME = "manual-m2"
FILE = "/tmp/opencode/manual-m2.txt"


class OperatorPty:
    """Stands in for the operator terminal."""

    def __init__(self):
        self.master, slave = pty.openpty()
        self.slave = slave
        self.buf = b""

    def spawn(self, args, env):
        return subprocess.Popen(args, env=env, stdin=self.slave,
                                stdout=self.slave, stderr=self.slave,
                                start_new_session=True)

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.buf += chunk

    def wait_for(self, marker: bytes, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in self.buf:
                return True
            self.pump(0.3)
        return False

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def text(self) -> str:
        return self.buf.decode(errors="replace")


def phase1_wrapper_launches_vim(env):
    print("== phase 1: cli-deck vim (wrapper prefix + takeover) ==")
    term = OperatorPty()
    proc = term.spawn([sys.executable, "-m", "cli_deck", "vim", FILE], env)
    os.close(term.slave)
    reached = term.wait_for(b"attached to")  # the wrapper's own attach hint
    print(f"wrapper attached the operator terminal: {reached}")
    term.pump(3.0)  # let vim finish its terminal init before typing
    print(f"vim screen reached the operator terminal: {FILE in term.text()}")

    term.send(b"i" + MARK.encode())  # INSERT, type
    # vim repaints with cursor escapes, so prove input landed by its mode line
    insert = term.wait_for(b"-- INSERT --", timeout=10)
    term.send(b"\x1b")               # ESC reaches vim (not a TUI binding)
    term.pump(1.0)
    print(f"keystrokes reached vim (-- INSERT -- shown): {insert}")

    term.send(deckd.TAKEOVER_DETACH_KEY)  # Ctrl-]
    try:
        rc = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        rc = None
    print(f"wrapper exited on Ctrl-] rc={rc}; vim keeps running: {pgrep_vim()}")
    term.pump(0.5)
    os.close(term.master)


def pgrep_vim() -> str:
    out = subprocess.run(["pgrep", "-af", f"vim {FILE}"],
                         capture_output=True, text=True).stdout.strip()
    return out or "(none)"


def phase2_listing(sock):
    print("== phase 2: cli-deck -l and the TUI table ==")
    listing = subprocess.run([sys.executable, "-m", "cli_deck", "-l"],
                             capture_output=True, text=True)
    print(f"cli-deck -l ->\n{listing.stdout.strip()}")
    return asyncio.run(_show_table(sock))


async def _show_table(sock):
    client = await deckd.DeckClient.connect(sock)
    app = DeckApp(client, sock)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(1.0)
            table = app.query_one(DataTable)
            for row in range(table.row_count):
                print(f"TUI row: {table.get_row_at(row)}")
            return table.get_row_at(0)[0]
    finally:
        await client.close()


def phase3_resume_replays(proc_id, sock):
    print("== phase 3: --resume takeover replays the vim screen ==")
    op_master, op_slave = pty.openpty()
    pump = threading.Thread(target=deckd.run_takeover,
                            args=(sock, proc_id, op_slave, op_slave), daemon=True)
    pump.start()
    time.sleep(2.0)
    data = b""
    while True:
        ready, _, _ = select.select([op_master], [], [], 0.3)
        if not ready:
            break
        data += os.read(op_master, 65536)
    os.write(op_master, deckd.TAKEOVER_DETACH_KEY)  # let the pump end cleanly
    pump.join(timeout=5)
    os.close(op_master)
    os.close(op_slave)
    screen = data.decode(errors="replace")
    print(f"replay looks like a vim screen (names the file): {FILE in screen}")
    print(f"replay carries the typed-into state (-- INSERT --): {'-- INSERT --' in screen}")
    print(f"pump ended on Ctrl-]: {not pump.is_alive()}")


def swap_file() -> Path:
    """vim keeps a swap file next to the target; a stale one blocks the demo."""
    directory, name = os.path.split(FILE)
    return Path(directory) / f".{name}.swp"


def main():
    os.makedirs("/tmp/opencode", exist_ok=True)
    sock, meta = deckd.deck_paths(NAME)
    env = {**os.environ, "CLI_DECK_NAME": NAME, "TERM": "xterm-256color",
           "M2_DEMO_VAR": "m2-env-visible"}
    swap_file().unlink(missing_ok=True)
    Path(FILE).write_text("seed line\n")

    phase1_wrapper_launches_vim(env)
    time.sleep(0.5)
    proc_id = phase2_listing(sock)

    phase3_resume_replays(proc_id, sock)

    print("== cleanup: kill vim, stop daemon ==")
    asyncio.run(_stop(sock, proc_id))
    deadline = time.monotonic() + 15
    while (sock.exists() or meta.exists()) and time.monotonic() < deadline:
        time.sleep(0.3)
    print(f"socket exists: {sock.exists()}  meta exists: {meta.exists()}")
    print(f"pgrep vim: {pgrep_vim()}")
    Path(FILE).unlink(missing_ok=True)
    swap_file().unlink(missing_ok=True)


async def _stop(sock, proc_id):
    client = await deckd.DeckClient.connect(sock)
    try:
        await client.send({"cmd": "kill", "id": proc_id})
        await asyncio.sleep(2.0)
        await client.send({"cmd": "stop"})
    finally:
        await client.close()


if __name__ == "__main__":
    main()
