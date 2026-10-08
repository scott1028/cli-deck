# cli-deck

A control center for interactive CLI processes: a Textual TUI lists them, a
detached daemon owns them, and later a wrapper prefix will let any command be
launched under the deck.

```text
Textual TUI (app.py, disposable)  <-- JSON lines over AF_UNIX -->  daemon (deckd.py) -> pty_host.py
full-screen takeover (t / Ctrl-]) <-- own AF_UNIX connection -->   same daemon pty
```

Closing the TUI kills nothing: the daemon is spawned with
`start_new_session=True` and ignores SIGHUP, so processes outlive it.

## Install / run

```sh
make deps      # uv sync (dev environment)
make install   # uv tool install .  -> `cli-deck` on PATH
make test      # unittest suite
make uninstall
```

```sh
cli-deck            # start (spawns the daemon if needed) and open the TUI
cli-deck --resume   # re-attach to a running deck (no spawn)
cli-deck -l         # list live decks: name, socket, pid, age
```

`-l` lists **decks**, not processes, so the operator can pick one to
`--resume`. A deck is live when the pid recorded in its metadata still exists;
entries whose pid is gone are swept (see Teardown). Deck name comes from
`CLI_DECK_NAME` (default `default`).

## TUI keys

| key | action |
|---|---|
| `a` | add a process (prompt: empty = plain `bash -i`, otherwise `bash -ic <cmd>` so aliases/functions/PATH apply) |
| `enter` | detail screen: **read-only** live follow + scrollback |
| `t` | **full-screen takeover** of the selected process's pty (from list or detail screen) |
| `Ctrl-]` | **(takeover) detach** and return to the TUI; the process keeps running |
| `x` | kill (escalates SIGINT -> SIGTERM -> SIGKILL on the process group) |
| `d` | detach: close the TUI, daemon keeps running |
| `q` | stop daemon (asks for confirmation first, kills its processes) |
| `b` | leave the detail screen (back to the list) |
| `esc` | **not a navigation key**: forwarded to the process's pty, so vim and friends still get it |

Takeover suspends the Textual app (`App.suspend()`), puts the operator
terminal in raw mode, and pumps bytes both ways between the terminal and the
daemon's master fd over its own daemon connection — vim, ssh, claude, codex
render natively. The detach key is `Ctrl-]` (`TAKEOVER_DETACH_KEY`); every
other byte, ESC included, goes to the child. The pty is resized to the operator
terminal on takeover, on attach, and whenever the terminal is resized.

## Teardown

`stop` (the `q` action), SIGINT or SIGTERM on the daemon all run the same path:

1. stop accepting connections (`Server.close()` + bounded `wait_closed()`),
2. cancel and await client handler tasks (bounded),
3. detach the pty readers, then kill every owned process group **concurrently**
   (SIGINT -> SIGTERM -> SIGKILL, 1s grace each, bounded reap),
4. close the pty masters and log files, unlink socket + metadata,
5. leave the process.

Every step is bounded and the whole teardown has a 5s watchdog
(`TEARDOWN_BUDGET`) that force-exits if cleanup ever wedges; the daemon then
calls `os._exit()` instead of running interpreter teardown, so a parked thread
cannot keep a stopped daemon alive as a ghost. Measured: ~0.1s for a normal
child, ~2s when children ignore SIGINT and SIGTERM.

Kill work runs on the daemon's own thread pool, never `asyncio.to_thread`:
the loop's default executor is joined during `asyncio.run()` teardown, which is
exactly how a stopped daemon used to linger.

A daemon killed hard (SIGKILL, crash) never runs that path, so its
`<digest>.json` / `<digest>.sock` pair would linger forever. Every daemon
startup sweeps such entries: if the pid recorded in a metadata file no longer
exists, the pair is removed. `cli-deck -l` sweeps too.

## Runtime files

- socket: `/tmp/cli-deck-<uid>/<sha256(name)[:12]>.sock` (dir `0700`, socket `0600`)
- metadata: sibling `.json` with `{socket, pid, started_at, log_dir, name}`
  (used by `--resume` and `-l`)
- **deck log dir: `/tmp/cli-deck-<uid>/<sha256(name)[:12]>.logs/<id>.log`** —
  one raw output log per process, deck-scoped and stable, flushed while the
  process runs so it can be tailed. Override with `CLI_DECK_LOG_DIR`, deck name
  via `CLI_DECK_NAME`
- per-process ring buffer (~200 KB) is replayed to the detail screen on attach

## Protocol (JSON lines over AF_UNIX)

Client -> daemon:

```json
{"cmd": "add", "command": "...", "argv": [...], "name": "...", "cwd": "..."}
{"cmd": "kill", "id": "..."}
{"cmd": "list"}
{"cmd": "attach", "id": "..."}            // id null = unsubscribe
{"cmd": "attach_input", "id": "...", "data": "<base64>"}
{"cmd": "winsize", "id": "...", "rows": 24, "cols": 80}
{"cmd": "stop"}
```

Daemon -> client:

```json
{"msg": "snapshot", "processes": [{"id","name","argv","cwd","state","exit_code","started_at","log_path"}]}
{"msg": "attached", "id": "...", "replay": "<base64 ring buffer>"}
{"msg": "log", "id": "...", "data": "<base64>"}
{"msg": "notice", "message": "..."}
{"msg": "error", "message": "..."}
```

A fresh connection always receives a `snapshot` immediately. Takeover opens a
second connection and only uses `attach` / `attach_input` / `winsize`, so the
protocol is unchanged.

Clients must drain unread messages before closing (`DeckClient.close()` does):
closing with data still in the receive queue makes the kernel send RST, which
drops commands the daemon has not read yet — including a final `stop`.

## Layout

```
cli_deck/
├── __init__.py
├── __main__.py      # start / --resume / -l (deck list) / hidden --daemon
├── app.py           # Textual TUI (disposable client) + takeover wiring
├── deckd.py         # daemon + protocol + DeckClient + run_takeover + paths
│                    # + sweep_stale_decks / list_decks
├── pty_host.py      # openpty + Popen, winsize, bounded kill escalation
└── registry.py      # process records + ring buffers
tests/               # pty, registry, protocol, takeover pump, signals,
                     # teardown, deck files, TUI
```

## Status / next milestones

M1 (done): daemon, pty hosting, TUI, takeover, resume, tests.

Deliberately not built yet (seams left in place):

- `cli-deck <cmd>` wrapper prefix (`add` already accepts explicit `argv`)
- `~/.bashrc.d` install and `BASH_FUNC_x%%` env passing
- inline VT emulator (detail screen is read-only follow, not a full VT;
  use `t` takeover for full-screen programs)
