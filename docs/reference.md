# cli-deck — full reference

- [Architecture](#architecture)
- [Running a command under the deck](#running-a-command-under-the-deck)
- [Caller environment](#caller-environment)
- [Shell wrapper](#shell-wrapper)
- [Dashboard key details](#dashboard-key-details)
- [Protection](#protection)
- [Teardown](#teardown)
- [Runtime files](#runtime-files)
- [Protocol](#protocol)
- [Layout](#layout)
- [Status / next milestones](#status--next-milestones)

## Architecture

```text
Textual TUI (app.py, disposable)  <-- JSON lines over AF_UNIX -->  daemon (deckd.py) -> pty_host.py
full-screen takeover (t / Ctrl-]) <-- own AF_UNIX connection -->   same daemon pty
```

Closing the TUI kills nothing: the daemon is spawned with
`start_new_session=True` and ignores SIGHUP, so processes outlive it.

## Running a command under the deck

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

## Caller environment

The daemon's environment is not the caller's, so the wrapper sends its `cwd` and
its whole environment as an overlay on the `add` command, and the daemon uses
that for the child. That carries what `bash -ic` + `~/.bashrc` cannot re-derive:
session exports (`export FOO=bar`), exported bash functions (`BASH_FUNC_myfn%%`)
and PATH edits. **Env values are never logged** — not to the daemon log, the
registry, or the process log (child output only). The socket stays `0600`, the
runtime dir `0700`.

## Shell wrapper

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

## Dashboard key details

| key | action |
|---|---|
| `a` | add a process (prompt: empty = plain `bash -i`, otherwise `bash -ic <cmd>` so aliases/functions/PATH apply) |
| `enter` | detail screen: **read-only** live follow + scrollback (keys are NOT sent to the process); plain text — terminal control sequences are stripped |
| `t` | **full-screen takeover** of the selected process's pty (from list or detail screen) |
| `Ctrl-]` | **(takeover) detach** and return to the TUI; the process keeps running |
| `x` | stop/remove: `running` rows ask first (`Stop '<name>'?`, default **No** — Enter/Escape cancel; Yes escalates SIGINT -> SIGTERM -> SIGKILL on the process group); `stopping` rows kill directly; `exited` rows are removed directly and a bottom-right toast shows where the kept log file is |
| `r` | restart: stop the process, then start a new row with the same command, cwd and env overlay — remove + add, so the row gets a new id and the old raw log file is kept; the new row takes the old row's position. `running` rows ask first (default **No**); `stopping`/`exited` rows restart directly |
| `p` | protect: toggle auto-restart for the selected row — when the process exits **on its own** the daemon restarts it in place; `x` stop, `r` restart, terminate-first remove and `q` never trigger it. Unprotected rows open a two-field dialog: max consecutive quick restarts (run < 10s; empty = 5, `-1` = unlimited) and restart gap in seconds (empty = 5, min 0.1, no upper bound) — `tab` switches fields, Enter submits, empty = that field's default. Protected rows turn protection off directly (see Protection) |
| `d` | detach: close the TUI, daemon keeps running |
| `q` | stop daemon: asks first when there are entries (they get killed); with no entries on the dashboard it leaves right away |
| `b` | leave the detail screen (back to the list) |
| `esc` | **not a navigation key**: forwarded to the process's pty, so vim and friends still get it |

Confirmation dialogs (the `x` stop, `r` and `q` prompts) focus **No** by default so
Enter cancels; `left` focuses Yes and `right` focuses No, stopping at the ends
(no cycling); Enter/Escape behave as before.

Both screens carry a persistent bottom bar, below the table/log and visually
separated from them. The dashboard bar splits the two view modes — `enter` =
read-only logs (keys not sent) vs `t` = interactive full-screen terminal
(`Ctrl-]` returns here) — from the other actions (`a`, `x` stop/remove, `r`
restart, `p` protect, `d` keeps processes, `q` kills them). The detail
screen's bar names the process (plain text, clipped to one line), states
READ-ONLY / keys-not-sent, and carries `t` interactive (`Ctrl-]` returns),
`b` back to the dashboard, and the `esc` exception, so the guidance is
visible inside the app.

Takeover suspends the Textual app (`App.suspend()`), puts the operator terminal
in raw mode, and pumps bytes both ways between the terminal and the daemon's
master fd over its own daemon connection — vim, ssh, claude, codex render
natively. The detach key is `Ctrl-]` (`TAKEOVER_DETACH_KEY`); every other byte,
ESC included, goes to the child. The pty is resized to the operator terminal on
takeover, on attach, and whenever the terminal is resized. On detach the pump
also resets any scroll region the child left behind (e.g. `watch`'s
`ESC[1;24r`) without moving the cursor, so the dashboard repaints correctly.

## Protection

With protection on, the daemon restarts a process whenever it exits **on its
own** — crash, `exit 0`, `exit` inside a takeover, vim `:q`. Tool-initiated
ends never trigger it: `x` stop, `r` restart and terminate-first `remove` all
go through the kill path, and `q`'s teardown detaches the pty readers before
the children die.

The restart reuses the respawn path: same position, new id, same name/argv/cwd
and env overlay, old raw log kept — and the new row keeps protection. A run
shorter than 10s counts as a **quick** restart; `p` opens a two-field dialog:
how many consecutive quick restarts are allowed (empty = 5, `-1` = unlimited,
`0` = only restart after a run of 10s or more) and the **restart gap** — the
seconds to wait before respawning (empty = 5, min 0.1, no upper bound), which
keeps a process that dies at startup from being restarted in a hot loop.
While the gap elapses the row stays `exited`, its protect column shows
`restart in 5s`, and a toast names the wait. Past the quick-restart limit
protection turns itself off, the row stays exited, and every client gets an
error toast naming the count.

`p`, `x`, `r` and `q` all cancel a restart still waiting on its gap: `p` on a
protected (or waiting) row turns protection off directly and the row stays
exited, `x` removes the row, `r` respawns once immediately (the new row keeps
protection and the gap, the quick count resets), and `q`'s teardown cancels
every pending restart without spawning anything new.

The daemon does the watching, so a daemon that dies takes protection down with
it. If a protected process restarts while you are in a takeover, press
`Ctrl-]` back to the dashboard and `t` the respawned row (new id) to re-attach.

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
- **log growth control (builtin, on by default)**: a raw log over
  `CLI_DECK_LOG_MAX_BYTES` (default 10 MiB) is capped in place — rewritten
  to its last half-budget on the same fd, so `tail -f` survives — and the
  deck log dir is then swept (also on every spawn): oldest logs of rows no
  longer on the dashboard are deleted until the dir is under
  `CLI_DECK_LOG_MAX_TOTAL_FACTOR * max_bytes` total bytes (default 10x) and
  at most `CLI_DECK_LOG_MAX_FILES` files (default 50). Logs of rows still
  on the dashboard (live or exited) are never swept. When
  `CLI_DECK_LOG_DIR` is set the dir sweep is off (the dir may be shared or
  hold other files); the per-file cap still applies.
  `CLI_DECK_LOG_MAX_BYTES=0` turns the builtin off entirely; OS logrotate
  can take over, but it must use `copytruncate` — the daemon holds the log
  fd open and never reopens the file
- per-process ring buffer (~200 KB) is replayed to the detail screen on attach

## Protocol (JSON lines over AF_UNIX)

Client -> daemon:

```json
{"cmd": "add", "command": "...", "argv": [...], "name": "...", "cwd": "...", "env": {...}}
{"cmd": "kill", "id": "..."}
{"cmd": "remove", "id": "...", "terminate_first": true}   // false only for exited records
{"cmd": "restart", "id": "..."}
{"cmd": "protect", "id": "...", "limit": 5, "gap": 5}   // limit null = off, -1 = unlimited; gap omitted/null = 5, >= 0.1
{"cmd": "list"}
{"cmd": "attach", "id": "..."}            // id null = unsubscribe
{"cmd": "attach_input", "id": "...", "data": "<base64>"}
{"cmd": "winsize", "id": "...", "rows": 24, "cols": 80}
{"cmd": "stop"}
```

Daemon -> client:

```json
{"msg": "snapshot", "processes": [{"id","name","argv","cwd","state","exit_code","started_at","log_path","protect_limit","protect_gap","protect_pending"}]}
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
detaches first (the raw log file is kept, subject to the log growth-control
sweep). Removing an `exited` record
answers the requesting client with a `notice` naming the kept log file when it
still exists. `restart` stops the process
(kill escalation) and then starts a new row with the same name, argv, cwd and
env overlay: the old entry is replaced in place (same position, new id) only
once the new child is up, and a `kill` or terminate-first `remove` cancels a
restart that is still waiting on the kill. `protect` sets or clears a row's
protection switch (`limit`: null = off, -1 = unlimited, N = at most N
consecutive restarts after quick (<10s) exits; `gap`: seconds an auto restart
waits first, omitted/null = 5, min 0.1): the daemon broadcasts a snapshot and
answers the requester with a notice; an accepted `protect` also cancels a
restart still waiting on its gap, a rejected one changes nothing. When a
protected row exits on its own the daemon marks it `protect_pending` and
respawns it after the gap (or turns protection off past the limit),
broadcasting a `notice` / `error` naming what happened. Takeover opens a
second connection and only uses `attach` /
`attach_input` / `winsize`. Clients must drain unread messages before closing
(`DeckClient.close()` does): closing with data still in the receive queue
makes the kernel send RST, which drops commands the daemon has not read yet —
including a final `stop`.

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
