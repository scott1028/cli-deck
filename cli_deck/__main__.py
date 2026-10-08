"""cli-deck entry point: start/attach the TUI, list decks, run the daemon."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from . import __version__
from .app import DeckApp
from .deckd import (DeckClient, deck_paths, ensure_daemon, format_age,
                    list_decks, run_daemon, socket_alive, sweep_stale_decks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli-deck",
        description="Control center for interactive CLI processes.",
    )
    parser.add_argument("--resume", action="store_true",
                        help="attach to an already running deck")
    parser.add_argument("-l", "--list", action="store_true",
                        help="list live decks (name, socket, pid, age) and exit")
    parser.add_argument("--version", action="version",
                        version=f"cli-deck {__version__}")
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.daemon:
        return run_daemon()
    if args.list:
        return _print_decks()
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


def _print_decks() -> int:
    """List live decks so the operator can pick one to --resume."""
    sweep_stale_decks()
    decks = list_decks()
    if not decks:
        print("no live decks (start one with `cli-deck`)", file=sys.stderr)
        return 1
    for deck in sorted(decks, key=lambda d: d.get("started_at", 0)):
        age = format_age(time.time() - float(deck.get("started_at", time.time())))
        print(f"{deck.get('name', '?'):<16} {deck['socket']:<44} "
              f"pid={deck['pid']:<7} age={age}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
