"""cli-deck entry point: start/attach the TUI, list processes, run the daemon."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from . import __version__
from .app import DeckApp
from .deckd import (DeckClient, deck_paths, ensure_daemon, run_daemon,
                    socket_alive)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli-deck",
        description="Control center for interactive CLI processes.",
    )
    parser.add_argument("--resume", action="store_true",
                        help="attach to an already running deck")
    parser.add_argument("-l", "--list", action="store_true",
                        help="list processes and exit")
    parser.add_argument("--version", action="version",
                        version=f"cli-deck {__version__}")
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.daemon:
        return run_daemon()
    if args.list:
        return asyncio.run(_print_list())
    return asyncio.run(_run_tui(resume=args.resume))


async def _run_tui(resume: bool) -> int:
    socket_path, meta_path = deck_paths()
    if resume:
        if not await socket_alive(socket_path):
            print("no running deck to resume (start one with `cli-deck`)",
                  file=sys.stderr)
            return 1
    else:
        await ensure_daemon(socket_path, meta_path)
    client = await DeckClient.connect(socket_path)
    try:
        await DeckApp(client, socket_path).run_async()
    finally:
        await client.close()
    return 0


async def _print_list() -> int:
    socket_path, _ = deck_paths()
    if not await socket_alive(socket_path):
        print("no running deck (start one with `cli-deck`)", file=sys.stderr)
        return 1
    client = await DeckClient.connect(socket_path)
    try:
        await client.send({"cmd": "list"})
        snapshot = await _next_snapshot(client)
    finally:
        await client.close()
    processes = snapshot.get("processes", [])
    if not processes:
        print("no processes")
        return 0
    for p in sorted(processes, key=lambda p: p.get("started_at", 0)):
        started = time.strftime("%H:%M:%S", time.localtime(p.get("started_at", 0)))
        exit_code = "" if p.get("exit_code") is None else f" exit={p['exit_code']}"
        print(f"{p['id']}  {p['state']:<8} {started}  {p['name']}{exit_code}")
    return 0


async def _next_snapshot(client: DeckClient) -> dict:
    while True:
        msg = await client.messages.get()
        if msg.get("msg") == "snapshot":
            return msg


if __name__ == "__main__":
    raise SystemExit(main())
