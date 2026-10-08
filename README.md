# cli-deck

A control center for interactive CLI processes: a Textual TUI lists them, a
detached daemon owns them, and `cli-deck <command>` launches any command under it.

```text
Textual TUI (app.py, disposable)  <-- JSON lines over AF_UNIX -->  daemon (deckd.py) -> pty_host.py
full-screen takeover (t / Ctrl-]) <-- own AF_UNIX connection -->   same daemon pty
```

Closing the TUI kills nothing: the daemon is spawned with
`start_new_session=True` and ignores SIGHUP, so processes outlive it.

## Install / run

```sh
make deps      # uv sync (dev environment)
make install   # uv tool install .  -> `cli-deck` on PATH, + the shell wrapper
make test      # unittest suite
make uninstall # reverses install; refuses while a daemon runs (FORCE=1 overrides)
```

```sh
cli-deck            # start (spawns the daemon if needed) and open the TUI
cli-deck --resume   # re-attach to a running deck (no spawn)
cli-deck -l         # list live decks: name, socket, pid, age
```

`-l` lists **decks**, not processes, so the operator can pick one to `--resume`.
A deck is live when the pid in its metadata still exists; entries whose pid is
gone are swept (see Teardown). Deck name comes from `CLI_DECK_NAME` (default `default`).

## Running a command under the deck

```sh
cli-deck vim notes.md      # starts it, then opens the dashboard with it selected
cli-deck ssh prod          # the process outlives the wrapper and this shell
cli-deck --no-launch make watch   # register only; print the attach recipe
```

The first argument that is not a known option starts the command; the rest is
joined into one string and run as `bash -ic <string>`, so `~/.bashrc` aliases,
functions and PATH apply. Every cli-deck option is a flag, so parsing stays
unambiguous; a command that must **start** with a flag is disambiguated with
`--`: `cli-deck -- -L 80:localhost:80`. `--resume`, `-l`, `--version`,
`--no-launch` and the hidden `--daemon` are unchanged. The wrapper then opens
the dashboard with the new process selected (older processes keep their rows);
it does **not** attach directly. Press `t` for full-screen takeover — `Ctrl-]`
returns to the dashboard — or `d` to close the dashboard; either way the
process keeps running and shows up in `-l` and in the TUI. Re-attach later
with `cli-deck --resume` (select the row, press `t`) — the takeover replays
the ring buffer first, so you see what happened while away. `cli-deck
<command>` and `--resume` open this same dashboard, so every takeover goes
through the Textual suspend/pump path described below.

`--no-launch` prints the recipe instead of opening the dashboard:

```text
deck:    default
socket:  /tmp/cli-deck-1000/f0011108146a.sock
id:      4890c5d1
command: make watch
cwd:     /home/scott/project
log:     /tmp/cli-deck-1000/f0011108146a.logs/4890c5d1.log
attach:  cli-deck --resume   (select the row, press t; Ctrl-] returns to the dashboard)
```

### Caller environment

The daemon's environment is not the caller's, so the wrapper sends its `cwd` and
its whole environment as an overlay on the `add` command, and the daemon uses
that for the child. That carries what `bash -ic` + `~/.bashrc` cannot re-derive:
session exports (`export FOO=bar`), exported bash functions (`BASH_FUNC_myfn%%`)
and PATH edits. **Env values are never logged** — not to the daemon log, the
registry, or the process log (child output only). The socket stays `0600`, the
runtime dir `0700`.

### Shell wrapper (`~/.bashrc.d`)

`make install` symlinks `scripts/cli-deck.bash` to `~/.bashrc.d/cli-deck`, which
defines a `cli-deck` **shell function**. It is needed because a function defined
only in your current shell is invisible to the daemon's child:

- if the first argument names a bash function, it is `export -f`'d first, so it
  travels as `BASH_FUNC_<name>%%` in the environment the wrapper forwards;
- the real binary is found with `type -P cli-deck` (which skips the function);
- `exec` is used only in non-interactive shells — in an interactive shell exec
  would replace the shell itself, and detaching would close it.

A fresh shell needs `~/.bashrc` to source `~/.bashrc.d/*` (the usual pattern),
otherwise `source ~/.bashrc.d/cli-deck`; an already-open shell keeps the old
function until it restarts (`unset -f cli-deck` clears it).

## TUI keys

| key | action |
|---|---|
| `a` | add a process (prompt: empty = plain `bash -i`, otherwise `bash -ic <cmd>` so aliases/functions/PATH apply) |
| `enter` | detail screen: **read-only** live follow + scrollback (keys are NOT sent to the process) |
| `t` | **full-screen takeover** of the selected process's pty (from list or detail screen) |
| `Ctrl-]` | **(takeover) detach** and return to the TUI; the process keeps running |
| `x` | kill (escalates SIGINT -> SIGTERM -> SIGKILL on the process group) |
| `r` | remove the selected entry from the daemon's registry: `exited` rows are removed directly; live rows ask first (default **No** — Enter/Escape cancel; Yes terminates, waits for exit, then removes). The raw log file is kept |
| `d` | detach: close the TUI, daemon keeps running |
| `q` | stop daemon (asks for confirmation first, kills its processes) |
| `b` | leave the detail screen (back to the list) |
| `esc` | **not a navigation key**: forwarded to the process's pty, so vim and friends still get it |

Both screens carry a persistent bottom bar, below the table/log and visually
separated from them. The dashboard bar splits the two view modes — `enter` =
read-only logs (keys not sent) vs `t` = interactive full-screen terminal
(`Ctrl-]` returns here) — from the other actions (`a`, `x`, `r` remove, `d`
keeps processes, `q` kills them). The detail screen's bar names the process (plain
text, clipped to one line), states READ-ONLY / keys-not-sent, and carries `t` interactive
(`Ctrl-]` returns), `b` back to the dashboard, and the `esc` exception, so the
guidance is visible inside the app.

Takeover suspends the Textual app (`App.suspend()`), puts the operator terminal
in raw mode, and pumps bytes both ways between the terminal and the daemon's
master fd over its own daemon connection — vim, ssh, claude, codex render
natively. The detach key is `Ctrl-]` (`TAKEOVER_DETACH_KEY`); every other byte,
ESC included, goes to the child. The pty is resized to the operator terminal on
takeover, on attach, and whenever the terminal is resized.

## Teardown

`stop` (the `q` action), SIGINT or SIGTERM on the daemon all run the same path:

1. stop accepting connections (`Server.close()` + bounded `wait_closed()`),
2. cancel and await client handler tasks (bounded),
3. detach the pty readers, then kill every owned process group **concurrently**
   (SIGINT -> SIGTERM -> SIGKILL, 1s grace each, bounded reap),
4. close the pty masters and log files, unlink socket + metadata,
5. leave the process.

Every step is bounded and the whole teardown has a 5s watchdog (`TEARDOWN_BUDGET`)
that force-exits if cleanup ever wedges; the daemon then calls `os._exit()`
instead of running interpreter teardown, so a parked thread cannot keep a stopped
daemon alive as a ghost. Measured: ~0.1s for a normal child, ~2s when children
ignore SIGINT and SIGTERM. Kill work runs on the daemon's own thread pool, never
`asyncio.to_thread`: the loop's default executor is joined during `asyncio.run()`
teardown, which is exactly how a stopped daemon used to linger. A daemon killed
hard (SIGKILL, crash) never runs that path, so its `<digest>.json` /
`<digest>.sock` pair would linger; every daemon startup and every `cli-deck -l`
sweeps pairs whose recorded pid no longer exists.

## Runtime files

- socket: `/tmp/cli-deck-<uid>/<sha256(name)[:12]>.sock` (dir `0700`, socket `0600`)
- metadata: sibling `.json` with `{socket, pid, started_at, log_dir, name}`
- **deck log dir: `/tmp/cli-deck-<uid>/<sha256(name)[:12]>.logs/<id>.log`** —
  one raw output log per process, deck-scoped and stable, flushed while the
  process runs so it can be tailed. Override with `CLI_DECK_LOG_DIR`
- per-process ring buffer (~200 KB) is replayed to the detail screen on attach

## Protocol (JSON lines over AF_UNIX)

Client -> daemon:

```json
{"cmd": "add", "command": "...", "argv": [...], "name": "...", "cwd": "...", "env": {...}}
{"cmd": "kill", "id": "..."}
{"cmd": "remove", "id": "...", "terminate_first": true}   // false only for exited records
{"cmd": "list"}
{"cmd": "attach", "id": "..."}            // id null = unsubscribe
{"cmd": "attach_input", "id": "...", "data": "<base64>"}
{"cmd": "winsize", "id": "...", "rows": 24, "cols": 80}
{"cmd": "stop"}
```

Daemon -> client:

```json
{"msg": "snapshot", "processes": [{"id","name","argv","cwd","state","exit_code","started_at","log_path"}]}
{"msg": "added", "id": "...", "name": "...", "cwd": "...", "log_path": "..."}
{"msg": "attached", "id": "...", "replay": "<base64 ring buffer>"}
{"msg": "log", "id": "...", "data": "<base64>"}
{"msg": "notice", "message": "..."}
{"msg": "error", "message": "..."}
```

A fresh connection always receives a `snapshot` immediately. `remove` with
`terminate_first: false` only drops `exited` records; live records require
`terminate_first: true`, and the daemon then reuses the kill escalation and
removes the entry once the child exits — even if the requesting client
detaches first (raw log files are never deleted). Takeover opens a
second connection and only uses `attach` / `attach_input` / `winsize`. Clients
must drain unread messages before closing (`DeckClient.close()` does): closing
with data still in the receive queue makes the kernel send RST, which drops
commands the daemon has not read yet — including a final `stop`.

## Layout

```
cli_deck/
├── __init__.py
├── __main__.py      # wrapper prefix / --resume / -l / --no-launch / --daemon
├── app.py           # Textual TUI (disposable client) + takeover wiring
├── deckd.py         # daemon + protocol + DeckClient + run_takeover + paths
│                    # + sweep_stale_decks / list_decks
├── pty_host.py      # openpty + Popen, winsize, bounded kill escalation
└── registry.py      # process records + ring buffers
scripts/cli-deck.bash  # shell function installed to ~/.bashrc.d/cli-deck
tests/               # pty, registry, protocol, takeover pump, signals, teardown,
                     # deck files, wrapper prefix, TUI
```

## Status / next milestones

M1 (done): daemon, pty hosting, TUI, takeover, resume, tests.
M2 (done): `cli-deck <cmd>` prefix, `--no-launch`, caller env overlay,
`~/.bashrc.d` wrapper install.

Deliberately not built yet (seams left in place):

- inline VT emulator (detail screen is read-only follow, not a full VT;
  use `t` takeover for full-screen programs)
- per-deck config files, remote/machine placement
