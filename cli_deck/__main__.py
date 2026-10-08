"""cli-deck entry point: TUI, deck list, wrapper launch, run the daemon."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from . import __version__
from .app import DeckApp
from .deckd import (DeckClient, deck_name, deck_paths, ensure_daemon,
                    format_age, list_decks, run_daemon, run_takeover,
                    socket_alive, sweep_stale_decks)

# Every cli-deck option is a flag (no option takes a value), which is what makes
# the `cli-deck <command...>` prefix form unambiguous.
OPTIONS = frozenset({"--resume", "-l", "--list", "--version", "--no-launch",
                     "--daemon", "-h", "--help"})


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, command = _split_wrapper(argv)
    args = _build_parser().parse_args(opts)

    if args.daemon:
        return run_daemon()
    if args.list:
        return _print_decks()
    if command:
        return asyncio.run(_run_wrapper(command, no_launch=args.no_launch))
    return asyncio.run(_run_tui(resume=args.resume))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-deck",
        description="Control center for interactive CLI processes.",
        epilog="`cli-deck <command...>` runs the command under the deck and "
               "attaches\n(Ctrl-] detaches, the process keeps running). Use `--` "
               "when the command\nstarts with a flag, e.g. `cli-deck -- -L 8080:x`.",
    )
    parser.add_argument("--resume", action="store_true",
                        help="attach to an already running deck")
    parser.add_argument("-l", "--list", action="store_true",
                        help="list live decks (name, socket, pid, age) and exit")
    parser.add_argument("--no-launch", action="store_true",
                        help="with a command: register it but do not attach; "
                             "print the attach recipe instead")
    parser.add_argument("--version", action="version",
                        version=f"cli-deck {__version__}")
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    return parser


def _split_wrapper(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv into cli-deck options and the command to run under the deck.

    The first token that is not a known flag starts the command, so
    `cli-deck ssh prod` runs `ssh prod`. A command that must start with a flag
    is disambiguated with `--`, e.g. `cli-deck -- -L 8080:...` or
    `cli-deck --resume -- ls`: everything after `--` is the command string.
    """
    opts: list[str] = []
    for index, token in enumerate(argv):
        if token == "--":
            return opts, argv[index + 1:]
        if token in OPTIONS:
            opts.append(token)
        elif token.startswith("-") and token != "-":
            opts.append(token)  # unknown option: argparse reports it, use `--`
        else:
            return opts, argv[index:]
    return opts, []


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


def _env_overlay() -> dict[str, str]:
    """The caller's environment, for the deck child. Never logged or stored.

    The daemon's env is not the caller's, so this carries session exports
    (`export FOO=bar`), exported bash functions (`BASH_FUNC_myfn%%`) and PATH
    edits. Entries that cannot be encoded for the wire are skipped.
    """
    overlay: dict[str, str] = {}
    for key, value in os.environ.items():
        try:
            (key + "=" + value).encode("utf-8")
        except UnicodeEncodeError:
            continue
        overlay[key] = value
    return overlay


async def _run_wrapper(command: list[str], no_launch: bool) -> int:
    """`cli-deck <command...>`: run it under the deck, then attach.

    Feels like tmux `new -A`: the command starts (or reuses) the deck, the
    operator terminal is handed to it, Ctrl-] detaches, and the process keeps
    running after this wrapper exits.
    """
    command_str = " ".join(command)
    socket_path, meta_path = deck_paths()
    await ensure_daemon(socket_path, meta_path)
    try:
        added = await _add_to_deck(socket_path, command_str)
    except RuntimeError as exc:
        print(f"cli-deck: {exc}", file=sys.stderr)
        return 1
    proc_id = added["id"]
    if no_launch:
        _print_recipe(command_str, proc_id, added, socket_path)
        return 0
    print(f"cli-deck: attached to '{command_str}' (id {proc_id}); "
          f"Ctrl-] detaches and the process keeps running", file=sys.stderr)
    run_takeover(socket_path, proc_id, sys.stdin.fileno(), sys.stdout.fileno())
    print(f"cli-deck: detached (id {proc_id}); "
          f"re-attach with `cli-deck --resume`", file=sys.stderr)
    return 0


async def _add_to_deck(socket_path, command_str: str) -> dict:
    """Register the command with the daemon; returns its `added` message."""
    client = await DeckClient.connect(socket_path)
    try:
        await client.send({"cmd": "add", "command": command_str,
                           "cwd": os.getcwd(), "env": _env_overlay()})
        while True:
            msg = await client.messages.get()
            if msg.get("msg") == "added":
                return msg
            if msg.get("msg") == "error":
                raise RuntimeError(msg.get("message", "add failed"))
    finally:
        await client.close()


def _print_recipe(command_str: str, proc_id: str, added: dict,
                  socket_path) -> None:
    print(f"deck:    {deck_name()}")
    print(f"socket:  {socket_path}")
    print(f"id:      {proc_id}")
    print(f"command: {command_str}")
    print(f"cwd:     {added.get('cwd', '')}")
    print(f"log:     {added.get('log_path', '')}")
    print("attach:  cli-deck --resume   (select the row, press t; Ctrl-] detaches)")


if __name__ == "__main__":
    raise SystemExit(main())
