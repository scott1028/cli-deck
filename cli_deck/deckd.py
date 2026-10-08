"""Detached daemon owning CLI processes, plus the JSON-lines client helpers.

Protocol (one JSON object per line, both directions):
  client -> daemon cmds: add, kill, list, attach, attach_input, winsize, stop
  daemon -> client msgs: snapshot, attached, log, notice, error
Binary payloads (pty input/output) travel base64-encoded.
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

from . import pty_host
from .registry import Registry, STATE_EXITED, STATE_STOPPING

SOCKET_CONNECT_TIMEOUT = 5.0
TAKEOVER_DETACH_KEY = b"\x1d"  # Ctrl-]


def runtime_dir() -> Path:
    return Path(f"/tmp/cli-deck-{os.getuid()}")


def deck_digest(name: str | None = None) -> str:
    name = name or os.environ.get("CLI_DECK_NAME", "default")
    return hashlib.sha256(name.encode()).hexdigest()[:12]


def deck_paths(name: str | None = None) -> tuple[Path, Path]:
    """Return (socket_path, meta_path) for a deck (env CLI_DECK_NAME, default "default")."""
    digest = deck_digest(name)
    rdir = runtime_dir()
    rdir.mkdir(mode=0o700, exist_ok=True)
    return rdir / f"{digest}.sock", rdir / f"{digest}.json"


def deck_log_dir(name: str | None = None) -> Path:
    """Deck-scoped, stable log dir (env CLI_DECK_LOG_DIR overrides)."""
    env = os.environ.get("CLI_DECK_LOG_DIR")
    if env:
        return Path(env)
    return runtime_dir() / f"{deck_digest(name)}.logs"


def read_meta(meta_path: Path) -> dict | None:
    try:
        return json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None


async def socket_alive(socket_path: Path) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)), timeout=1.0)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def ensure_daemon(socket_path: Path, meta_path: Path) -> dict:
    """Return daemon metadata, spawning a detached daemon when needed."""
    meta = read_meta(meta_path)
    if meta and await socket_alive(socket_path):
        return meta
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    subprocess.Popen(
        [sys.executable, "-m", "cli_deck", "--daemon"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + SOCKET_CONNECT_TIMEOUT
    while time.monotonic() < deadline:
        if await socket_alive(socket_path):
            meta = read_meta(meta_path)
            if meta:
                return meta
        await asyncio.sleep(0.1)
    raise RuntimeError(f"daemon did not come up on {socket_path}")


def snapshot_msg(registry: Registry) -> dict:
    return {"msg": "snapshot", "processes": [r.to_public() for r in registry.all()]}


class _ClientConn:
    """One connected TUI/CLI client."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.subscribed: str | None = None
        self._lock = asyncio.Lock()

    async def send(self, msg: dict) -> None:
        data = (json.dumps(msg) + "\n").encode()
        async with self._lock:
            try:
                self.writer.write(data)
                await self.writer.drain()
            except (ConnectionError, OSError):
                pass

    def send_nowait(self, msg: dict) -> None:
        """Fire-and-forget send for use from pty reader callbacks."""
        try:
            self.writer.write((json.dumps(msg) + "\n").encode())
        except (ConnectionError, OSError):
            return
        asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        try:
            await self.writer.drain()
        except (ConnectionError, OSError):
            pass


class DeckDaemon:
    def __init__(self, socket_path: Path, meta_path: Path, logs: Path) -> None:
        self.socket_path = socket_path
        self.meta_path = meta_path
        self.logs = logs
        self.registry = Registry()
        self.handles: dict[str, pty_host.PtyHandle] = {}
        self.log_files: dict[str, object] = {}
        self.clients: set[_ClientConn] = set()
        self._client_tasks: set[asyncio.Task] = set()
        self._kill_tasks: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        except ValueError:
            pass  # not the main thread (tests)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stopping.set)
            except (ValueError, RuntimeError, NotImplementedError):
                pass  # not the main thread (tests)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._on_client, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.meta_path.write_text(json.dumps({
            "socket": str(self.socket_path),
            "pid": os.getpid(),
            "started_at": time.time(),
            "log_dir": str(self.logs),
        }))
        try:
            await self._stopping.wait()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        for task in list(self._kill_tasks):
            await asyncio.wait({task}, timeout=10)
        for handle in list(self.handles.values()):
            await asyncio.to_thread(pty_host.kill, handle)
            pty_host.close(handle)
        for fh in self.log_files.values():
            fh.close()
        self.log_files.clear()
        for task in list(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
        if self._server is not None:
            self._server.close()
        for path in (self.socket_path, self.meta_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # -- client connections -------------------------------------------------

    async def _on_client(self, reader, writer) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
            task.add_done_callback(self._client_tasks.discard)
        conn = _ClientConn(reader, writer)
        self.clients.add(conn)
        try:
            await conn.send(snapshot_msg(self.registry))
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    await conn.send({"msg": "error", "message": "invalid json"})
                    continue
                await self._dispatch(conn, msg)
        except (ConnectionError, OSError):
            pass
        finally:
            self.clients.discard(conn)
            try:
                writer.close()
            except OSError:
                pass

    async def _dispatch(self, conn: _ClientConn, msg: dict) -> None:
        handlers = {
            "add": self._cmd_add,
            "kill": self._cmd_kill,
            "list": self._cmd_list,
            "attach": self._cmd_attach,
            "attach_input": self._cmd_attach_input,
            "winsize": self._cmd_winsize,
            "stop": self._cmd_stop,
        }
        handler = handlers.get(msg.get("cmd"))
        if handler is None:
            await conn.send({"msg": "error",
                             "message": f"unknown cmd: {msg.get('cmd')!r}"})
            return
        try:
            await handler(conn, msg)
        except Exception as exc:  # a bad command must not drop the connection
            await conn.send({"msg": "error", "message": f"{msg.get('cmd')}: {exc}"})

    # -- commands -----------------------------------------------------------

    async def _cmd_add(self, conn: _ClientConn, msg: dict) -> None:
        argv = msg.get("argv")
        command = msg.get("command")
        if argv:
            launch_argv = list(argv)
        elif command:
            launch_argv = ["bash", "-ic", command]  # cli form: aliases/PATH apply
        else:
            launch_argv = ["bash", "-i"]  # shell form
        name = msg.get("name") or (command or " ".join(launch_argv))
        cwd = msg.get("cwd") or os.getcwd()
        record = self.registry.create(name, launch_argv, cwd)
        log_path = self.logs / f"{record.id}.log"
        record.log_path = str(log_path)
        try:
            self.logs.mkdir(parents=True, exist_ok=True)
            self.log_files[record.id] = open(log_path, "wb")
            handle = pty_host.spawn(record.id, launch_argv, cwd)
        except OSError as exc:
            self.registry.remove(record.id)
            fh = self.log_files.pop(record.id, None)
            if fh is not None:
                fh.close()
            await conn.send({"msg": "error", "message": f"add failed: {exc}"})
            return
        self.handles[record.id] = handle
        asyncio.get_running_loop().add_reader(
            handle.master_fd, self._on_output, record.id)
        self._broadcast_snapshot()

    async def _cmd_kill(self, conn: _ClientConn, msg: dict) -> None:
        proc_id = msg.get("id")
        handle = self.handles.get(proc_id)
        record = self.registry.get(proc_id)
        if handle is None or record is None:
            await conn.send({"msg": "error",
                             "message": f"no such process: {proc_id!r}"})
            return
        record.state = STATE_STOPPING
        self._broadcast_snapshot()
        task = asyncio.create_task(asyncio.to_thread(pty_host.kill, handle))
        self._kill_tasks.add(task)
        task.add_done_callback(self._kill_tasks.discard)

    async def _cmd_list(self, conn: _ClientConn, msg: dict) -> None:
        await conn.send(snapshot_msg(self.registry))

    async def _cmd_attach(self, conn: _ClientConn, msg: dict) -> None:
        proc_id = msg.get("id")
        if proc_id is None:
            conn.subscribed = None
            await conn.send({"msg": "attached", "id": None})
            return
        record = self.registry.get(proc_id)
        if record is None:
            await conn.send({"msg": "error",
                             "message": f"no such process: {proc_id!r}"})
            return
        conn.subscribed = proc_id
        await conn.send({"msg": "attached", "id": proc_id,
                         "replay": base64.b64encode(record.ring.get()).decode()})

    async def _cmd_attach_input(self, conn: _ClientConn, msg: dict) -> None:
        handle = self.handles.get(msg.get("id"))
        if handle is None:
            await conn.send({"msg": "error",
                             "message": f"no live process: {msg.get('id')!r}"})
            return
        pty_host.write_input(handle, base64.b64decode(msg.get("data", "")))

    async def _cmd_winsize(self, conn: _ClientConn, msg: dict) -> None:
        handle = self.handles.get(msg.get("id"))
        if handle is None:
            await conn.send({"msg": "error",
                             "message": f"no live process: {msg.get('id')!r}"})
            return
        pty_host.set_winsize(handle, int(msg.get("rows", 24)), int(msg.get("cols", 80)))

    async def _cmd_stop(self, conn: _ClientConn, msg: dict) -> None:
        self._stopping.set()

    # -- output pump --------------------------------------------------------

    def _on_output(self, proc_id: str) -> None:
        handle = self.handles.get(proc_id)
        record = self.registry.get(proc_id)
        if handle is None or record is None:
            return
        chunk = pty_host.read_chunk(handle)
        if chunk is None:
            self._mark_exited(proc_id)
            return
        record.ring.append(chunk)
        fh = self.log_files.get(proc_id)
        if fh is not None:
            fh.write(chunk)
            fh.flush()  # keep the raw log tail-able while the process runs
        payload = base64.b64encode(chunk).decode()
        for conn in self.clients:
            if conn.subscribed == proc_id:
                conn.send_nowait({"msg": "log", "id": proc_id, "data": payload})

    def _mark_exited(self, proc_id: str) -> None:
        handle = self.handles.pop(proc_id, None)
        record = self.registry.get(proc_id)
        if handle is not None:
            try:
                asyncio.get_running_loop().remove_reader(handle.master_fd)
            except (OSError, ValueError):
                pass
            pty_host.close(handle)
            code = handle.poll()
            if code is None:
                try:
                    code = handle.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    code = None
            if record is not None:
                record.state = STATE_EXITED
                record.exit_code = code
        fh = self.log_files.pop(proc_id, None)
        if fh is not None:
            fh.close()
        self._broadcast_snapshot()

    def _broadcast_snapshot(self) -> None:
        msg = snapshot_msg(self.registry)
        for conn in list(self.clients):
            conn.send_nowait(msg)


class DeckClient:
    """Line-based JSON client; incoming daemon messages land in `messages`."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.messages: asyncio.Queue = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._read_loop())

    @classmethod
    async def connect(cls, socket_path: Path) -> "DeckClient":
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        return cls(reader, writer)

    async def _read_loop(self) -> None:
        while True:
            line = await self.reader.readline()
            if not line:
                self.messages.put_nowait({"msg": "notice",
                                          "message": "daemon closed the connection"})
                break
            try:
                self.messages.put_nowait(json.loads(line))
            except ValueError:
                self.messages.put_nowait({"msg": "error",
                                          "message": "invalid json from daemon"})

    async def send(self, msg: dict) -> None:
        self.writer.write((json.dumps(msg) + "\n").encode())
        await self.writer.drain()

    async def close(self) -> None:
        self._reader_task.cancel()
        try:
            await self._reader_task
        except (asyncio.CancelledError, Exception):
            pass
        # Drain unread incoming bytes first: closing with data still in the
        # receive queue makes the kernel send RST, which discards commands
        # we already wrote (e.g. a final stop).
        try:
            deadline = time.monotonic() + 0.5
            while not self.reader.at_eof() and time.monotonic() < deadline:
                data = await asyncio.wait_for(self.reader.read(65536), timeout=0.25)
                if not data:
                    break
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except OSError:
            pass


def term_winsize(fd: int) -> tuple[int, int]:
    """(rows, cols) of the terminal behind fd, with env fallback."""
    try:
        if fd >= 0:
            cols, rows = struct.unpack(
                "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
            if rows and cols:
                return rows, cols
    except (OSError, ValueError):
        pass
    size = shutil.get_terminal_size()
    return size.lines, size.columns


def run_takeover(socket_path: Path, proc_id: str, in_fd: int, out_fd: int,
                 detach_key: bytes = TAKEOVER_DETACH_KEY) -> None:
    """Full-screen takeover pump: raw-mode bytes between the operator
    terminal (in_fd/out_fd) and the daemon pty for proc_id.

    Runs on its own daemon connection so it works while the Textual event
    loop is blocked inside App.suspend(). Returns on detach_key, EOF, or
    daemon death; the deck process keeps running throughout.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(socket_path))

    def send(msg: dict) -> None:
        sock.sendall((json.dumps(msg) + "\n").encode())

    def send_input(data: bytes) -> None:
        send({"cmd": "attach_input", "id": proc_id,
              "data": base64.b64encode(data).decode()})

    rows, cols = term_winsize(in_fd)
    send({"cmd": "attach", "id": proc_id})
    send({"cmd": "winsize", "id": proc_id, "rows": rows, "cols": cols})

    is_tty = os.isatty(in_fd)
    old_attrs = termios.tcgetattr(in_fd) if is_tty else None
    buf = b""
    try:
        if is_tty:
            tty.setraw(in_fd)
        while True:
            # 0.5s tick so terminal resizes are picked up even with no input
            readable, _, _ = select.select([in_fd, sock.fileno()], [], [], 0.5)
            new_size = term_winsize(in_fd)
            if new_size != (rows, cols):
                rows, cols = new_size
                send({"cmd": "winsize", "id": proc_id, "rows": rows, "cols": cols})
            if in_fd in readable:
                try:
                    data = os.read(in_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                idx = data.find(detach_key)
                if idx >= 0:
                    if idx:
                        send_input(data[:idx])
                    break
                send_input(data)
            if sock.fileno() in readable:
                try:
                    chunk = os.read(sock.fileno(), 65536)
                except OSError:
                    break
                if not chunk:
                    break  # daemon gone
                buf += chunk
                while True:
                    line, sep, rest = buf.partition(b"\n")
                    if not sep:
                        break
                    buf = rest
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("msg") == "log" and msg.get("id") == proc_id:
                        os.write(out_fd, base64.b64decode(msg.get("data", "")))
    finally:
        if old_attrs is not None:
            termios.tcsetattr(in_fd, termios.TCSADRAIN, old_attrs)
        # Drain unread daemon messages (snapshot broadcasts) before closing:
        # an RST would discard the last attach_input still in flight.
        try:
            sock.setblocking(False)
            while sock.recv(65536):
                pass
        except (BlockingIOError, OSError):
            pass
        sock.close()


def run_daemon() -> int:
    socket_path, meta_path = deck_paths()
    asyncio.run(DeckDaemon(socket_path, meta_path, deck_log_dir()).run())
    return 0
