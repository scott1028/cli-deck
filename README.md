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
SIGINT/SIGTERM on the daemon runs the same clean stop path (kills its
processes, removes socket + metadata).

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
cli-deck -l         # print the process list and exit
```

## TUI keys

| key | action |
|---|---|
| `a` | add a process (prompt: empty = plain `bash -i`, otherwise `bash -ic <cmd>` so aliases/functions/PATH apply) |
| `enter` | detail screen: **read-only** live follow + scrollback |
| `t` | **full-screen takeover** of the selected process's pty (from list or detail screen) |
| `Ctrl-]` | (during takeover) detach and return to the TUI; the process keeps running |
| `x` | kill (escalates SIGINT -> SIGTERM -> SIGKILL on the process group) |
| `d` | detach: close the TUI, daemon keeps running |
| `q` | stop daemon (asks for confirmation first, kills its processes) |
| `esc` | leave the detail screen |

Takeover suspends the Textual app (`App.suspend()`), puts the operator
terminal in raw mode, and pumps bytes both ways between the terminal and the
daemon's master fd over its own daemon connection — vim, ssh, claude, codex
render natively. The pty is resized to the operator terminal on takeover, on
attach, and whenever the terminal is resized.

## Runtime files

- socket: `/tmp/cli-deck-<uid>/<sha256(name)[:12]>.sock` (dir `0700`, socket `0600`)
- metadata: sibling `.json` with `{socket, pid, started_at, log_dir}` (used by `--resume`)
- raw logs: `/tmp/cli-deck-<uid>/<sha256(name)[:12]>.logs/<id>.log`
  (deck-scoped and stable; override with `CLI_DECK_LOG_DIR`, deck name via `CLI_DECK_NAME`)
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

## Layout

```
cli_deck/
├── __init__.py
├── __main__.py      # start / --resume / -l / hidden --daemon
├── app.py           # Textual TUI (disposable client) + takeover wiring
├── deckd.py         # daemon + protocol + DeckClient + run_takeover + paths
├── pty_host.py      # openpty + Popen, winsize, kill escalation
└── registry.py      # process records + ring buffers
tests/               # pty, registry, protocol, takeover pump, signals, TUI
```

## Status / next milestones

M1 (done): daemon, pty hosting, TUI, takeover, resume, tests.

Deliberately not built yet (seams left in place):

- `cli-deck <cmd>` wrapper prefix (`add` already accepts explicit `argv`)
- `~/.bashrc.d` install and `BASH_FUNC_x%%` env passing
- inline VT emulator (detail screen is read-only follow, not a full VT;
  use `t` takeover for full-screen programs)
