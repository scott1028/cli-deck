"""cli-deck entry point: TUI, deck list, wrapper launch, run the daemon."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from . import __version__
from .app import DeckApp, parse_protect_settings
from .deckd import (LOG_MAX_BYTES_DEFAULT, LOG_MAX_FILES_DEFAULT,
                    LOG_TOTAL_FACTOR_DEFAULT, PROTECT_GAP_DEFAULT,
                    PROTECT_LIMIT_DEFAULT, DeckClient, deck_name,
                    deck_paths, ensure_daemon, format_age, list_decks,
                    run_daemon, socket_alive, sweep_stale_decks)

# Row actions consume an ID; protection values must use `=`. The first bare
# token after those options starts the command.
VALUE_OPTIONS = frozenset({"--stop", "--restart", "--rm", "--up", "--unprotect"})
OPTIONS = frozenset({"-r", "--resume", "-l", "--list", "--version", "-d", "--detach",
                     "-p", "--protect",
                     "--daemon", "-h", "--help"}) | VALUE_OPTIONS


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, command = _split_wrapper(argv)
    parser = _build_parser()
    args = parser.parse_args(opts)

    if args.detach and args.resume:
        parser.error("--detach cannot be combined with --resume")
    if any(getattr(args, action) is not None
           for action in ("stop", "restart", "rm", "up")):
        if command or args.detach or args.protect is not None or args.resume:
            parser.error("row actions cannot be combined with a command, "
                         "--detach, --protect or --resume")
        try:
            return asyncio.run(asyncio.wait_for(_run_row_action(args), 5))
        except (OSError, asyncio.TimeoutError) as exc:
            print(f"cli-deck: {str(exc) or 'row action timed out'}",
                  file=sys.stderr)
            return 1
    if args.protect is not None and not command:
        parser.error("--protect requires a command")

    if args.daemon:
        return run_daemon()
    if args.list:
        return _print_decks()
    if command:
        return asyncio.run(_run_wrapper(command, detach=args.detach,
                                        protect=args.protect))
    if args.detach:
        socket_path, meta_path = deck_paths()
        asyncio.run(ensure_daemon(socket_path, meta_path))
        print(f"deck:    {deck_name()}")
        print(f"socket:  {socket_path}")
        print("attach:  cli-deck --resume")
        return 0
    return asyncio.run(_run_tui(resume=args.resume))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-deck",
        description="Control center for interactive CLI processes.",
        # raw formatter: the hand-aligned environment list keeps its layout
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="`cli-deck <command...>` runs the command under the deck and "
               "opens the\ndashboard with it selected (press t for full-screen, "
               "Ctrl-]\nreturns). Use `--` when the command starts with a flag, "
               "e.g.\n`cli-deck -- -L 8080:x`. Glue -p values with `=` "
               "(`-p=-1,2`); after a\nspace the value is read as the command."
               "\n\n"
               "Row actions (--stop, --restart, --rm, --up) act on the deck "
               "named by\nCLI_DECK_NAME; `cli-deck -l` shows row IDs. --stop "
               "and --rm refuse a\nprotected row. To retire one: --up ID, "
               "--stop ID, then --rm ID once it\nhas exited.\n\n"
               "environment:\n"
               "  CLI_DECK_NAME                 deck name (default: "
               "`default`);\n"
               "                                one daemon per name\n"
               "  CLI_DECK_LOG_DIR              raw log dir (default:\n"
               "                                `/tmp/cli-deck-<uid>/"
               "<hash>.logs`); when set,\n"
               "                                old logs are never swept\n"
               "  CLI_DECK_LOG_MAX_BYTES        per-log size cap in bytes "
               f"(default {LOG_MAX_BYTES_DEFAULT});\n"
               "                                0 turns all log size "
               "control off\n"
               "  CLI_DECK_LOG_MAX_TOTAL_FACTOR dir byte budget = factor x "
               "max\n"
               "                                bytes (default "
               f"{LOG_TOTAL_FACTOR_DEFAULT}); 0 = off\n"
               "  CLI_DECK_LOG_MAX_FILES        max log files kept in the "
               f"dir (default {LOG_MAX_FILES_DEFAULT});\n"
               "                                0 = off\n\n"
               "The log knobs are read when the deck daemon starts.",
    )
    parser.add_argument("-r", "--resume", action="store_true",
                        help="attach to an already running deck")
    parser.add_argument("-l", "--list", action="store_true",
                        help="list live decks (name, socket, pid, age) and "
                             "their processes, then exit")
    parser.add_argument("-d", "--detach", action="store_true",
                        help="with a command: register it and print the attach "
                             "recipe instead of opening the dashboard")
    parser.add_argument("-p", "--protect", nargs="?",
                        const=(PROTECT_LIMIT_DEFAULT, PROTECT_GAP_DEFAULT),
                        type=_parse_protect, metavar="LIMIT[,GAP]",
                        help="protect a new command; set values with =")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--stop", metavar="ID", help="stop an unprotected row")
    actions.add_argument("--restart", metavar="ID", help="restart a row")
    actions.add_argument("--rm", metavar="ID", help="remove an exited row")
    actions.add_argument("--up", "--unprotect", metavar="ID",
                         help="turn off protection for a row")
    parser.add_argument("--version", action="version",
                        version=f"cli-deck {__version__}")
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    return parser


def _parse_protect(settings: str) -> tuple[int, float]:
    limit, _, gap = settings.partition(",")
    try:
        return parse_protect_settings(limit, gap)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _split_wrapper(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv into cli-deck options and the command to run under the deck.

    Row actions keep their next token as the ID. The first bare token starts
    the command, so
    `cli-deck ssh prod` runs `ssh prod`. A command that must start with a flag
    is disambiguated with `--`, e.g. `cli-deck -- -L 8080:...` or
    `cli-deck --resume -- ls`: everything after `--` is the command string.
    """
    opts: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return opts, argv[index + 1:]
        if token in VALUE_OPTIONS:
            opts.extend(argv[index:index + 2])
            index += 2
            continue
        if token in OPTIONS:
            opts.append(token)
        elif token.startswith("-") and token != "-":
            opts.append(token)  # unknown option: argparse reports it, use `--`
        else:
            return opts, argv[index:]
        index += 1
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


async def _run_row_action(args) -> int:
    socket_path, _ = deck_paths()
    if not await socket_alive(socket_path):
        print("no running deck (start one with `cli-deck`)", file=sys.stderr)
        return 1
    action, proc_id = next((action, getattr(args, action))
                           for action in ("stop", "restart", "rm", "up")
                           if getattr(args, action) is not None)
    client = await DeckClient.connect(socket_path)
    try:
        snapshot = await client.messages.get()
        row = next((row for row in snapshot["processes"]
                    if row["id"] == proc_id), None)
        if row is None:
            print(f"no such process: {proc_id!r}; use cli-deck -l to find IDs",
                  file=sys.stderr)
            return 1
        name, state = row["name"], row["state"]
        if action in ("stop", "rm") and row.get("protect_limit") is not None:
            print(f"'{name}' ({proc_id}) is protected; turn it off first: "
                  f"cli-deck --up {proc_id}", file=sys.stderr)
            return 1
        if action == "rm" and state in ("running", "stopping"):
            print(f"'{name}' is {state}; stop it first: "
                  f"cli-deck --stop {proc_id}", file=sys.stderr)
            return 1
        if action == "stop" and state == "exited":
            print(f"'{name}' ({proc_id}) already exited")
            return 0
        command = {"cmd": {"stop": "kill", "restart": "restart",
                            "rm": "remove", "up": "protect"}[action],
                   "id": proc_id}
        if action == "rm":
            command["terminate_first"] = False
        elif action == "up":
            command["limit"] = None
        await client.send(command)
        # The fence follows every response to the action on this connection.
        await client.send({"cmd": "attach", "id": None})
        has_error = False
        has_notice = False
        while True:
            msg = await client.messages.get()
            if msg.get("msg") == "attached":
                break
            if msg.get("msg") == "error":
                print(msg["message"], file=sys.stderr)
                has_error = True
            elif msg.get("msg") == "notice":
                print(msg["message"])
                has_notice = True
        if has_error:
            return 1
        if not has_notice:
            confirmation = {"stop": "stopping", "restart": "restarting",
                            "rm": "removed", "up": "protection off for"}[action]
            print(f"{confirmation} '{name}' ({proc_id})")
        return 0
    finally:
        await client.close()


def _print_decks() -> int:
    """List live decks and their processes so the operator can pick one to
    --resume or a row ID to act on."""
    sweep_stale_decks()
    decks = sorted(list_decks(), key=lambda d: d.get("started_at", 0))
    if not decks:
        print("no live decks (start one with `cli-deck`)", file=sys.stderr)
        return 1
    snapshots = asyncio.run(_read_deck_processes([d["socket"] for d in decks]))
    for deck, processes in zip(decks, snapshots):
        age = format_age(time.time() - float(deck.get("started_at", time.time())))
        print(f"{deck.get('name', '?'):<16} {deck['socket']:<44} "
              f"pid={deck['pid']:<7} age={age}")
        if processes is None:
            print("  (unreachable)")
            continue
        if not processes:
            print("  (no processes)")
            continue
        # dashboard columns, name last so long names keep the others aligned
        rows = [("id", "state", "exit", "started", "protect", "name")]
        for process in processes:
            proc_id, name, *cells = DeckApp._row_cells(process)
            rows.append((proc_id, *cells, name))
        widths = [max(len(row[col]) for row in rows) for col in range(5)]
        for row in rows:
            print("  " + "".join(f"{cell:<{width}}  "
                                 for cell, width in zip(row, widths)) + row[5])
    return 0


async def _read_deck_processes(sockets: list[str]) -> list[list[dict] | None]:
    """Each deck's processes from its first snapshot; None if unreachable."""
    results: list[list[dict] | None] = []
    for socket_path in sockets:
        try:
            results.append(await asyncio.wait_for(_read_processes(socket_path), 2))
        except (OSError, asyncio.TimeoutError):
            results.append(None)
    return results


async def _read_processes(socket_path: str) -> list[dict] | None:
    client = await DeckClient.connect(socket_path)
    try:
        snapshot = await client.messages.get()  # sent first on every connection
    finally:
        await client.close()
    return snapshot.get("processes")


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


async def _run_wrapper(command: list[str], detach: bool,
                       protect: tuple[int, float] | None = None) -> int:
    """`cli-deck <command...>`: run it under the deck, then open the dashboard.

    The command starts (or reuses) the deck and the dashboard opens with the
    new process selected; `t` hands the terminal to it full-screen (Ctrl-]
    returns to the dashboard), `d` closes the dashboard, and the process
    keeps running after this wrapper exits.
    """
    command_str = " ".join(command)
    socket_path, meta_path = deck_paths()
    await ensure_daemon(socket_path, meta_path)
    try:
        added = await _add_to_deck(socket_path, command_str, protect)
    except RuntimeError as exc:
        print(f"cli-deck: {exc}", file=sys.stderr)
        return 1
    proc_id = added["id"]
    if detach:
        _print_recipe(command_str, proc_id, added, socket_path, protect)
        return 0
    client = await DeckClient.connect(socket_path)
    try:
        await DeckApp(client, socket_path, select_id=proc_id).run_async()
    finally:
        await client.close()
    return 0


async def _add_to_deck(socket_path, command_str: str,
                       protect: tuple[int, float] | None = None) -> dict:
    """Register the command with the daemon; returns its `added` message."""
    client = await DeckClient.connect(socket_path)
    try:
        await client.send({"cmd": "add", "command": command_str,
                           "cwd": os.getcwd(), "env": _env_overlay()})
        while True:
            msg = await client.messages.get()
            if msg.get("msg") == "added":
                added = msg
                break
            if msg.get("msg") == "error":
                raise RuntimeError(msg.get("message", "add failed"))
        if protect is not None:
            limit, gap = protect
            await client.send({"cmd": "protect", "id": added["id"],
                               "limit": limit, "gap": gap})
            while True:
                msg = await client.messages.get()
                if msg.get("msg") == "notice":
                    break
                if msg.get("msg") == "error":
                    raise RuntimeError(msg.get("message", "protect failed"))
        return added
    finally:
        await client.close()


def _print_recipe(command_str: str, proc_id: str, added: dict,
                  socket_path, protect: tuple[int, float] | None = None) -> None:
    print(f"deck:    {deck_name()}")
    print(f"socket:  {socket_path}")
    print(f"id:      {proc_id}")
    print(f"command: {command_str}")
    print(f"cwd:     {added.get('cwd', '')}")
    print(f"log:     {added.get('log_path', '')}")
    if protect is not None:
        limit, gap = protect
        label = "∞" if limit == -1 else str(limit)  # same as the protect column
        print(f"protect: on ({label}, {gap:g}s)")
    print("attach:  cli-deck --resume   (select the row, press t; "
          "Ctrl-] returns to the dashboard)")


if __name__ == "__main__":
    raise SystemExit(main())
