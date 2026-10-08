"""Pty-backed process hosting: spawn, output pump, winsize, kill escalation."""
from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import time
from dataclasses import dataclass

DEFAULT_ROWS = 24
DEFAULT_COLS = 80

# SIGINT -> SIGTERM -> SIGKILL on the process group; None = no further wait.
KILL_ESCALATION: list[tuple[signal.Signals, float | None]] = [
    (signal.SIGINT, 2.0),
    (signal.SIGTERM, 2.0),
    (signal.SIGKILL, None),
]


@dataclass
class PtyHandle:
    id: str
    argv: list[str]
    cwd: str
    proc: subprocess.Popen
    master_fd: int
    rows: int
    cols: int

    def poll(self) -> int | None:
        return self.proc.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.proc.wait(timeout=timeout)


def spawn(proc_id: str, argv: list[str], cwd: str | None = None,
          rows: int = DEFAULT_ROWS, cols: int = DEFAULT_COLS) -> PtyHandle:
    """Start argv in a new session with a pty as its controlling terminal."""
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, rows, cols)
    workdir = cwd or os.getcwd()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=workdir,
            start_new_session=True,
        )
    finally:
        os.close(slave_fd)
    return PtyHandle(id=proc_id, argv=list(argv), cwd=workdir,
                     proc=proc, master_fd=master_fd, rows=rows, cols=cols)


def read_chunk(handle: PtyHandle, size: int = 65536) -> bytes | None:
    """Return available output, or None when the pty is closed (child gone)."""
    try:
        return os.read(handle.master_fd, size)
    except OSError as exc:
        if exc.errno in (errno.EIO, errno.ENXIO):
            return None
        raise


def write_input(handle: PtyHandle, data: bytes) -> None:
    try:
        os.write(handle.master_fd, data)
    except OSError:
        pass  # child already gone


def set_winsize(handle: PtyHandle, rows: int, cols: int) -> None:
    _set_winsize(handle.master_fd, rows, cols)
    handle.rows, handle.cols = rows, cols


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def kill(handle: PtyHandle, escalation: list = KILL_ESCALATION) -> int | None:
    """Escalate signals on the child's process group; return the exit code."""
    try:
        pgid = os.getpgid(handle.proc.pid)
    except ProcessLookupError:
        return handle.poll()
    for sig, grace in escalation:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        if grace is None:
            break
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            code = handle.poll()
            if code is not None:
                return code
            time.sleep(0.05)
    return handle.wait()


def close(handle: PtyHandle) -> None:
    try:
        os.close(handle.master_fd)
    except OSError:
        pass


def read_until_exit(handle: PtyHandle, timeout: float = 20.0) -> tuple[bytes, int]:
    """Collect all output until the child exits (test helper)."""
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"process {handle.id} did not exit in {timeout}s")
        ready, _, _ = select.select([handle.master_fd], [], [], min(remaining, 0.2))
        if ready:
            chunk = read_chunk(handle)
            if chunk is None:
                break
            chunks.append(chunk)
        elif handle.poll() is not None:
            chunk = read_chunk(handle)  # drain buffered output
            if chunk is None:
                break
            chunks.append(chunk)
    code = handle.wait(timeout=max(0.1, deadline - time.monotonic()))
    return b"".join(chunks), code
