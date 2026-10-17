# cli-deck

[English](README.en.md) · [繁體中文](README.md) · [日本語](README.ja.md)

![cli-deck mascot](docs/mascot.png)

**Keep your terminal programs alive when you close the window — and jump back
into them later, exactly as you left them.**

Think of a babysitter (the daemon): it holds your programs while you are away.
The full-screen dashboard is just a remote control you can put down any time.

| Feature | How it works | Example |
|---|---|---|
| **Programs outlive the window** | A small background helper owns the programs, not your terminal. Closing the dashboard kills nothing. | `cli-deck ssh prod`, then close the terminal |
| **Jump back in** | Open the dashboard again, select the row, press `t`: full-screen takeover, your keystrokes in, the program's screen out. | `cli-deck --resume` |
| **Your shell stays your shell** | Commands run through `bash -ic`, so your aliases, functions and PATH apply. | `cli-deck vim notes.md` |
| **See what happened while away** | The last ~200 KB of screen output replays when you re-attach. | press `t` |
| **Watch without touching** | `enter` opens a read-only live view with scrollback; keys never reach the program. | press `enter` |
| **Auto-restart (protection)** | `p` marks a row; if the program dies on its own, the helper restarts it. | press `p` |
| **Logs kept, capped** | Every process gets a raw log file; growth is capped so it never fills your disk. | `--no-launch` prints the log path |

## Install

### From source

With uv installed, run this from the repository root:

```sh
make install   # uv tool install .  -> `cli-deck` on PATH, + the shell wrapper
```

Open a new shell (or `source ~/.bashrc.d/cli-deck`) so the wrapper function
loads. `make uninstall` reverses it; `make deps` sets up the dev environment.
[Wrapper details](docs/reference.md#shell-wrapper).

### AppImage (Linux)

Download an AppImage from [Releases](https://github.com/scott1028/cli-deck/releases):
`x86_64` for Intel/AMD, or `aarch64` for ARM. It includes Python; uv is not needed.
Rename the download to `cli-deck.AppImage`, then install it:

```sh
install -Dm755 cli-deck.AppImage ~/.local/bin/cli-deck
```

Without FUSE, prefix commands with `APPIMAGE_EXTRACT_AND_RUN=1`.

### macOS (tarball)

Download a tarball from [Releases](https://github.com/scott1028/cli-deck/releases):
`macos-aarch64` for Apple Silicon, or `macos-x86_64` for Intel. It includes Python;
uv is not needed. Extract it wherever you like and put the launcher on PATH:

```sh
tar xzf cli-deck-macos-aarch64.tar.gz -C ~/.local/share/cli-deck
ln -sf ~/.local/share/cli-deck/bin/cli-deck ~/.local/bin/cli-deck
```

If macOS blocks the first run, clear the download flag:
`xattr -dr com.apple.quarantine ~/.local/share/cli-deck`.

## Start in one line

```sh
cli-deck vim notes.md    # starts vim under the deck and opens the dashboard
```

Close the terminal whenever you like — vim keeps running. Later:

```sh
cli-deck --resume        # re-attach to a running deck (or: cli-deck -r)
cli-deck -l              # list live decks: name, socket, pid, age
```

Pick the vim row and press `t` to take over the screen. `Ctrl-]` drops you back
to the dashboard. The program never stops in between.

## Common commands

| Command | What it does |
|---|---|
| `cli-deck` | open the dashboard (starts the helper if needed) |
| `cli-deck <command>` | start a command under the deck, then open the dashboard |
| `cli-deck -r` / `--resume` | re-attach to a running deck |
| `cli-deck -l` | list live decks so you can pick one to resume |
| `cli-deck --no-launch <cmd>` | register the command only; print the attach recipe |
| `cli-deck -- -L 80:localhost:80` | `--` marks "everything after this is my command" |

`-l` lists **decks**, not processes. A deck is live when its recorded pid still
exists; dead entries are swept. The deck name comes from `CLI_DECK_NAME`
(default `default`).

## Dashboard keys

| key | action |
|---|---|
| `a` | add a process (empty = plain bash, otherwise your command) |
| `enter` | read-only live output + scrollback (keys are not sent) |
| `t` | full-screen takeover of the selected process |
| `Ctrl-]` | leave takeover, back to the dashboard |
| `x` | stop or remove (asks first for running rows; default No) |
| `r` | restart: same command, cwd and environment, new row id |
| `p` | protect: auto-restart when the process dies on its own |
| `d` | close the dashboard; everything keeps running |
| `q` | stop the helper (asks first — running processes get killed) |
| `b` | back to the list |
| `esc` | goes to the program, so vim and friends still work |

Confirmation dialogs focus **No** by default, so Enter cancels.
[Key-by-key behavior](docs/reference.md#dashboard-key-details).

## How it works (short version)

Two parts: a detached daemon that owns the processes in pseudo-terminals, and a
disposable Textual TUI that talks to it over a Unix socket. The daemon runs in
its own session and ignores SIGHUP, so closing the TUI (or your whole terminal)
kills nothing. Takeover suspends the TUI and pumps raw bytes both ways between
your terminal and the program's pty — that is why vim, ssh and htop render
natively.

## Develop

```sh
make deps    # uv sync
make test    # unittest suite
```

Pushing a higher `version` in `pyproject.toml` to `master` runs the tests,
builds the Linux AppImages and the macOS tarballs, and publishes a release
tagged `v<version>-<first 8 commit characters>`.

## More details

- [Full reference](docs/reference.md): architecture, protocol, protection,
  teardown, runtime files, environment variables, and repo layout.
