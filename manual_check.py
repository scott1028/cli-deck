"""Headless M1 verification: real daemon + real TUI driven via run_test.

Flow: start TUI -> add bash -i -> detail follow (read-only) -> d=detach ->
pgrep -> takeover-typing via run_takeover -> cli-deck -l -> --resume path ->
replay check -> q=stop daemon -> pgrep.
"""
import asyncio
import base64
import os
import pty
import select
import subprocess
import sys
import threading
import time

from cli_deck import deckd
from cli_deck.app import DeckApp
from textual.widgets import DataTable

MARK = "manual-check-42"


class TracingApp(DeckApp):
    """Records everything the daemon streams (live log + attach replay)."""

    def __init__(self, client, socket_path):
        super().__init__(client, socket_path)
        self.captured = b""

    def handle_daemon_message(self, msg):
        if msg.get("msg") == "log":
            self.captured += base64.b64decode(msg.get("data", ""))
        elif msg.get("msg") == "attached":
            self.captured += base64.b64decode(msg.get("replay", "") or "")
        super().handle_daemon_message(msg)


def pgrep_bash_i():
    out = subprocess.run(["pgrep", "-af", "bash -i"],
                         capture_output=True, text=True)
    return out.stdout.strip() or "(none)"


async def deckd_process_ids(sock):
    """Ask the daemon for its process ids (-l now lists decks, not processes)."""
    client = await deckd.DeckClient.connect(sock)
    try:
        await client.send({"cmd": "list"})
        while True:
            msg = await client.messages.get()
            if msg.get("msg") == "snapshot" and msg.get("processes"):
                return [p["id"] for p in msg["processes"]]
    finally:
        await client.close()


async def phase1(sock, meta):
    print("== phase 1: start TUI, add bash -i, detail follow, detach ==")
    m = await deckd.ensure_daemon(sock, meta)
    print(f"daemon pid={m['pid']} socket={sock}")
    client = await deckd.DeckClient.connect(sock)
    app = TracingApp(client, sock)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        await pilot.press("a")            # add
        await pilot.pause()
        await pilot.press("enter")        # empty command -> plain bash -i
        await pilot.pause(2.0)
        table = app.query_one(DataTable)
        print(f"table row after add: {table.get_row_at(0)}")
        await pilot.press("enter")        # enter = read-only detail follow
        await pilot.pause(1.0)
        print(f"detail screen open: {type(app.screen).__name__}")
        print(f"bash prompt followed live: {b'$' in app.captured}")
        await pilot.press("b")            # b = back to the list (esc goes to the pty)
        await pilot.pause()
        await pilot.press("d")            # detach: TUI closes, daemon stays
    await client.close()
    print("TUI closed via d=detach")


def takeover_type(sock, proc_id, text):
    """Type into the process via the takeover pump on a pty operator pair."""
    op_master, op_slave = pty.openpty()
    pump = threading.Thread(target=deckd.run_takeover,
                            args=(sock, proc_id, op_slave, op_slave),
                            daemon=True)
    pump.start()
    time.sleep(0.5)
    os.write(op_master, text.encode())
    time.sleep(0.5)
    os.write(op_master, deckd.TAKEOVER_DETACH_KEY)  # Ctrl-]
    pump.join(timeout=5)
    data = b""
    while True:
        readable, _, _ = select.select([op_master], [], [], 0.2)
        if not readable:
            break
        data += os.read(op_master, 65536)
    os.close(op_master)
    os.close(op_slave)
    return pump.is_alive(), data


async def phase2(sock, meta):
    print("== phase 2: --resume path, replay check, q=stop daemon ==")
    alive = await deckd.socket_alive(sock)
    print(f"socket_alive (what --resume checks): {alive}")
    if not alive:
        raise SystemExit("FAIL: daemon not alive for resume")
    client = await deckd.DeckClient.connect(sock)
    app = TracingApp(client, sock)
    async with app.run_test() as pilot:
        await pilot.pause(1.0)
        table = app.query_one(DataTable)
        print(f"resumed table row: {table.get_row_at(0)}")
        await pilot.press("enter")        # detail -> attach replays ring buffer
        await pilot.pause(1.0)
        text = app.captured.decode(errors="replace")
        lines = [ln for ln in text.splitlines() if MARK in ln]
        print(f"replay contains earlier '{MARK}': {MARK in text}")
        print(f"replayed lines: {lines}")
        await pilot.press("b")            # back to the list
        await pilot.pause()
        await pilot.press("q")            # stop daemon (confirm first)
        await pilot.pause()
        print(f"confirm screen: {type(app.screen).__name__}")
        await pilot.press("enter")        # Stop button
        await pilot.pause(1.0)
    await client.close()


def main():
    sock, meta = deckd.deck_paths()
    asyncio.run(phase1(sock, meta))
    time.sleep(0.5)
    print(f"pgrep 'bash -i' after detach: {pgrep_bash_i()}")

    listing = subprocess.run([sys.executable, "-m", "cli_deck", "-l"],
                             capture_output=True, text=True)
    print(f"cli-deck -l (live decks) ->\n{listing.stdout.strip()}")
    proc_id = asyncio.run(deckd_process_ids(sock))[0]
    print(f"deck processes: {proc_id}")
    logs = deckd.deck_log_dir()
    print(f"deck log dir: {logs}")
    print(subprocess.run(["ls", "-l", str(logs)],
                         capture_output=True, text=True).stdout.strip())

    print(f"typing '{MARK}' into the live bash via takeover pump...")
    pump_alive, data = takeover_type(sock, proc_id, f"echo {MARK}\r")
    print(f"pump detached on Ctrl-]: {not pump_alive}")
    print(f"takeover echoed '{MARK}': {MARK in data.decode(errors='replace')}")

    asyncio.run(phase2(sock, meta))
    deadline = time.monotonic() + 15  # kill escalation takes a few seconds
    while sock.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    print("== after stop ==")
    print(f"socket exists: {sock.exists()}  meta exists: {meta.exists()}")
    print(f"pgrep 'bash -i': {pgrep_bash_i()}")


if __name__ == "__main__":
    main()
