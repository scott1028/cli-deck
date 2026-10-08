"""End-to-end takeover proof: the REAL TUI runs under a pty; keys are typed
into it; `t` takes over a vim session; Ctrl-] returns; `d` detaches.

Proves: full-screen program renders through the takeover into the operator
terminal, typed keys reach it, the process runs throughout.
"""
import asyncio
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

from cli_deck import deckd

OUTFILE = "/tmp/opencode/m1_vim.txt"
DECK = "manual-vim"


def read_avail(fd, seconds):
    buf = b""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.2)
        if readable:
            try:
                buf += os.read(fd, 65536)
            except OSError:
                break
    return buf


def main():
    if os.path.exists(OUTFILE):
        os.remove(OUTFILE)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    env = {**os.environ, "TERM": "xterm-256color", "CLI_DECK_NAME": DECK}
    proc = subprocess.Popen([sys.executable, "-m", "cli_deck"],
                            stdin=slave, stdout=slave, stderr=slave,
                            env=env, start_new_session=True)
    os.close(slave)

    def send(data, wait=1.0):
        os.write(master, data)
        return read_avail(master, wait)

    out = read_avail(master, 2.5)
    print(f"TUI up under pty: {len(out)} bytes of screen output")

    out = send(b"a", 0.5)                       # a = add
    send(b"vim -u NONE -N", 0.5)
    out = send(b"\r", 3.0)                      # launch bash -ic 'vim ...'
    print("list shows vim row:", b"vim -u NONE -N" in out)

    out = send(b"\r", 1.0)                      # enter = detail (read-only)
    out = send(b"t", 3.0)                       # t = takeover
    print("vim full-screen output on operator terminal:",
          b"\x1b[" in out and b"~" in out)
    pg = subprocess.run(["pgrep", "-af", "vim -u NONE"],
                        capture_output=True, text=True)
    print(f"vim running during takeover: {pg.stdout.strip() or '(none)'}")

    out = send(b"iTAKEOVER-VIM-OK", 0.8)        # type into vim
    out += send(b"\x1b", 0.5)                   # normal mode
    out += send(f":w! {OUTFILE}\r".encode(), 1.0)
    out += send(b":q\r", 2.0)                   # quit vim
    print("vim echoed typed text:", b"TAKEOVER-VIM-OK" in out)

    out = send(b"\x1d", 1.5)                    # Ctrl-] -> back to TUI
    print("back in TUI after Ctrl-]:", b"cli-deck" in out or b"vim" in out)

    out = send(b"d", 1.0)                       # d = detach TUI
    proc.wait(timeout=10)
    print("TUI exited via d=detach, daemon still up:",
          asyncio.run(deckd.socket_alive(deckd.deck_paths(DECK)[0])))

    print("vim wrote file with typed text:",
          os.path.exists(OUTFILE)
          and "TAKEOVER-VIM-OK" in open(OUTFILE).read())

    listing = subprocess.run([sys.executable, "-m", "cli_deck", "-l"],
                             capture_output=True, text=True, env=env)
    print(f"cli-deck -l ->\n{listing.stdout.strip()}")

    async def stop():
        sock, _ = deckd.deck_paths(DECK)
        client = await deckd.DeckClient.connect(sock)
        await client.send({"cmd": "stop"})
        await asyncio.sleep(1)
        await client.close()
    asyncio.run(stop())
    sock, meta = deckd.deck_paths(DECK)
    print(f"after stop: socket exists={sock.exists()} meta exists={meta.exists()}")
    os.close(master)


if __name__ == "__main__":
    main()
