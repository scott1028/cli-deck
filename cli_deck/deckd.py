"""Detached daemon owning CLI processes, plus the JSON-lines client helpers.

Protocol (one JSON object per line, both directions):
  client -> daemon cmds: add, kill, remove, restart, protect, list, attach,
  attach_input, winsize, stop
  daemon -> client msgs: snapshot, added, attached, log, notice, error
Binary payloads (pty input/output) travel base64-encoded. The `add` cmd may
carry `cwd` and an `env` overlay for the child; env values are never logged.
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import math
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
import traceback
import tty
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import pty_host
from .registry import (Registry, ProcessRecord, STATE_EXITED, STATE_RUNNING,
                       STATE_STOPPING)

SOCKET_CONNECT_TIMEOUT = 5.0
TAKEOVER_DETACH_KEY = b"\x1d"  # Ctrl-]

# Protection (auto-restart when a process exits on its own): a run shorter
# than PROTECT_QUICK_EXIT counts as one quick restart; a row may take at most
# protect_limit consecutive quick restarts (-1 = unlimited) before protection
# turns itself off. Each scheduled restart first waits protect_gap seconds
# (default PROTECT_GAP_DEFAULT, at least PROTECT_GAP_MIN, no upper bound).
# Read at call time so tests can retune the threshold.
PROTECT_LIMIT_DEFAULT = 5
PROTECT_QUICK_EXIT = 10.0
PROTECT_GAP_DEFAULT = 5.0
PROTECT_GAP_MIN = 0.1

# Teardown must be deterministic and bounded: every step below has its own
# timeout, and the sum stays inside TEARDOWN_BUDGET so a daemon always leaves
# within ~5s of `stop`/SIGINT/SIGTERM instead of lingering as a ghost.
TEARDOWN_BUDGET = 5.0
SERVER_CLOSE_TIMEOUT = 1.0
CLIENT_CLOSE_TIMEOUT = 1.0
KILL_CLOSE_TIMEOUT = 3.0


def runtime_dir() -> Path:
    return Path(f"/tmp/cli-deck-{os.getuid()}")


def deck_name(name: str | None = None) -> str:
    return name or os.environ.get("CLI_DECK_NAME", "default")


def deck_digest(name: str | None = None) -> str:
    return hashlib.sha256(deck_name(name).encode()).hexdigest()[:12]


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


# Log growth control (builtin, on by default): per-file in-place truncate,
# then a dir sweep by total bytes / file count. 0 disables the matching knob;
# CLI_DECK_LOG_MAX_BYTES=0 turns the whole builtin off, leaving the dir to OS
# logrotate (which needs copytruncate: the daemon holds the fd, never reopens).
LOG_MAX_BYTES_DEFAULT = 10 * 1024 * 1024
LOG_TOTAL_FACTOR_DEFAULT = 10  # dir byte budget = factor * max_bytes
LOG_MAX_FILES_DEFAULT = 50


def _env_knob(name: str, default: int) -> int:
    """Non-negative int env knob; blank/garbage/negative falls back to default."""
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def log_max_bytes() -> int:
    """Per-log-file cap (env CLI_DECK_LOG_MAX_BYTES; 0 = unlimited)."""
    return _env_knob("CLI_DECK_LOG_MAX_BYTES", LOG_MAX_BYTES_DEFAULT)


def log_total_factor() -> int:
    """Dir byte budget is factor * max_bytes (env CLI_DECK_LOG_MAX_TOTAL_FACTOR;
    0 = byte condition off)."""
    return _env_knob("CLI_DECK_LOG_MAX_TOTAL_FACTOR", LOG_TOTAL_FACTOR_DEFAULT)


def log_max_files() -> int:
    """Dir file count cap (env CLI_DECK_LOG_MAX_FILES; 0 = count condition off)."""
    return _env_knob("CLI_DECK_LOG_MAX_FILES", LOG_MAX_FILES_DEFAULT)


def read_meta(meta_path: Path) -> dict | None:
    try:
        return json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None


def pid_alive(pid: object) -> bool:
    """True when `pid` is an existing process we can signal."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def sweep_stale_decks(rdir: Path | None = None) -> list[Path]:
    """Remove <digest>.json/.sock pairs whose recorded daemon pid is gone.

    A daemon killed hard (SIGKILL, crash) never runs its stop path, so its
    files would otherwise pile up in the runtime dir forever.
    """
    rdir = rdir or runtime_dir()
    removed: list[Path] = []
    try:
        meta_paths = sorted(rdir.glob("*.json"))
    except OSError:
        return removed
    for meta_path in meta_paths:
        meta = read_meta(meta_path)
        if meta is None:  # not ours to judge: leave unreadable files alone
            continue
        if pid_alive(meta.get("pid")):
            continue
        removed.append(meta_path)
        for path in (meta_path, rdir / f"{meta_path.stem}.sock"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return removed


def list_decks(rdir: Path | None = None) -> list[dict]:
    """Metadata of every deck whose recorded daemon pid is still alive."""
    rdir = rdir or runtime_dir()
    decks: list[dict] = []
    try:
        meta_paths = sorted(rdir.glob("*.json"))
    except OSError:
        return decks
    for meta_path in meta_paths:
        meta = read_meta(meta_path)
        if meta is None or not pid_alive(meta.get("pid")):
            continue
        meta.setdefault("name", meta_path.stem)
        meta.setdefault("socket", str(rdir / f"{meta_path.stem}.sock"))
        decks.append(meta)
    return decks


def format_age(seconds: float) -> str:
    """Age of a deck for `cli-deck -l`: 45s / 12m03s / 3h15m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


async def _bounded(awaitable, timeout: float) -> None:
    """Await `awaitable`, giving up after `timeout` instead of hanging."""
    try:
        await asyncio.wait_for(awaitable, timeout)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass


def _child_env(overlay: object) -> dict[str, str] | None:
    """Environment for a deck child: the caller's overlay, never the daemon's.

    The overlay carries what `bash -ic` + ~/.bashrc cannot re-derive: session
    exports (FOO=bar), exported functions (BASH_FUNC_myfn%%) and PATH edits.
    Values are never logged or stored in the registry.
    """
    if not isinstance(overlay, dict) or not overlay:
        return None
    env = {str(key): str(value) for key, value in overlay.items()}
    env.setdefault("TERM", "xterm-256color")
    return env


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
        self._drain_tasks: set[asyncio.Task] = set()

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
        task = asyncio.ensure_future(self._drain())
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)

    def cancel_pending(self) -> None:
        """Cancel fire-and-forget drains so teardown has nothing left to wait on."""
        for task in list(self._drain_tasks):
            task.cancel()

    async def _drain(self) -> None:
        try:
            await self.writer.drain()
        except (ConnectionError, OSError):
            pass


class DeckDaemon:
    def __init__(self, socket_path: Path, meta_path: Path, logs: Path,
                 name: str | None = None, hard_exit: bool = False,
                 log_max_bytes: int = 0, log_total_factor: int = 0,
                 log_max_files: int = 0) -> None:
        self.socket_path = socket_path
        self.meta_path = meta_path
        self.logs = logs
        self.name = deck_name(name)
        # hard_exit is only set for the real daemon process: teardown then has
        # a watchdog that force-exits, so no wedged cleanup can keep it alive.
        self.hard_exit = hard_exit
        # log growth control (0 = off; run_daemon passes the env knobs):
        # per-file truncate, then a dir sweep by total bytes / file count
        self.log_max_bytes = log_max_bytes
        self.log_total_factor = log_total_factor
        self.log_max_files = log_max_files
        self._log_written: dict[str, int] = {}
        self.registry = Registry()
        self.handles: dict[str, pty_host.PtyHandle] = {}
        self.log_files: dict[str, object] = {}
        self.clients: set[_ClientConn] = set()
        self._client_tasks: set[asyncio.Task] = set()
        # In-flight kill futures: held so they are not garbage collected while
        # running; teardown re-kills whatever is still alive, it does not join.
        self._kill_tasks: set[asyncio.Future] = set()
        # ids whose removal the daemon must finish once the current kill ends
        # (daemon-owned: survives the requesting client detaching)
        self._remove_after_exit: set[str] = set()
        # ids the daemon must respawn once the current kill ends
        self._restart_after_exit: set[str] = set()
        # protection restarts waiting on their gap: id -> timer handle
        self._pending_restarts: dict[str, asyncio.TimerHandle] = {}
        # per-id env overlays kept outside the registry: env values are never
        # stored in records or logs, but a restart needs them to respawn
        self._child_envs: dict[str, dict[str, str] | None] = {}
        self._server: asyncio.Server | None = None
        self._stopping = asyncio.Event()
        # Our own pool: the loop's default executor is joined by asyncio.run()
        # teardown, so a kill thread parked there keeps the process alive.
        self._executor = ThreadPoolExecutor(max_workers=8,
                                            thread_name_prefix="deck-kill")

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
            "name": self.name,
        }))
        try:
            await self._stopping.wait()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        loop = asyncio.get_running_loop()
        self._clear_signal_handlers(loop)
        watchdog = (loop.call_later(TEARDOWN_BUDGET, self._force_exit)
                    if self.hard_exit else None)
        try:
            await self._teardown(loop)
        finally:
            if watchdog is not None:
                watchdog.cancel()
            self._remove_files()

    async def _teardown(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._server is not None:
            self._server.close()
            await _bounded(self._server.wait_closed(), SERVER_CLOSE_TIMEOUT)
        for conn in self.clients:
            conn.cancel_pending()
        for task in list(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            await _bounded(asyncio.gather(*self._client_tasks,
                                          return_exceptions=True),
                           CLIENT_CLOSE_TIMEOUT)
        handles = list(self.handles.values())
        self.handles.clear()
        for handle in handles:
            self._detach_reader(handle)  # no callbacks while the children die
        # no protection restart may fire during teardown: cancel every pending
        # timer in this synchronous block (readers gone means no new
        # _mark_exited, so no new timers; a child a timer spawned before this
        # point is already in the handles snapshot and gets killed below)
        for timer in self._pending_restarts.values():
            timer.cancel()
        self._pending_restarts.clear()
        if handles:
            kills = [loop.run_in_executor(self._executor, pty_host.kill, handle)
                     for handle in handles]
            _, pending = await asyncio.wait(kills, timeout=KILL_CLOSE_TIMEOUT)
            for future in pending:
                future.cancel()
        for handle in handles:
            pty_host.close(handle)
        for fh in self.log_files.values():
            fh.close()
        self.log_files.clear()
        self._log_written.clear()
        # Never join kill threads: they are bounded, and the process exits right
        # after this, so a parked thread cannot turn into a ghost daemon.
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _force_exit(self) -> None:
        """Teardown overran its budget: drop our files and leave the process."""
        self._remove_files()
        os._exit(0)

    def _remove_files(self) -> None:
        for path in (self.socket_path, self.meta_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _clear_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Restore default dispositions so a second SIGINT/SIGTERM can end a
        teardown that has wedged."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except (ValueError, RuntimeError, NotImplementedError):
                pass

    def _detach_reader(self, handle: pty_host.PtyHandle) -> None:
        try:
            asyncio.get_running_loop().remove_reader(handle.master_fd)
        except (OSError, ValueError):
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
            "remove": self._cmd_remove,
            "restart": self._cmd_restart,
            "protect": self._cmd_protect,
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
        child_env = _child_env(msg.get("env"))  # caller overlay; never logged
        try:
            record = self._spawn_record(name, launch_argv, cwd, child_env)
        except OSError as exc:
            await conn.send({"msg": "error", "message": f"add failed: {exc}"})
            return
        self._broadcast_snapshot()
        await conn.send({"msg": "added", "id": record.id,
                         "name": name, "cwd": cwd, "log_path": record.log_path})

    def _spawn_record(self, name: str, argv: list[str], cwd: str,
                      child_env: dict[str, str] | None) -> ProcessRecord:
        """Create the record, open its log, spawn the pty; raises OSError."""
        record = self.registry.create(name, argv, cwd)
        log_path = self.logs / f"{record.id}.log"
        record.log_path = str(log_path)
        try:
            self.logs.mkdir(parents=True, exist_ok=True)
            # w+b: the truncate path preads the tail, so the fd must be readable
            self.log_files[record.id] = open(log_path, "w+b")
            handle = pty_host.spawn(record.id, argv, cwd, env=child_env)
        except OSError:
            self.registry.remove(record.id)
            fh = self.log_files.pop(record.id, None)
            if fh is not None:
                fh.close()
            raise
        self.handles[record.id] = handle
        self._child_envs[record.id] = child_env
        self._log_written[record.id] = 0
        asyncio.get_running_loop().add_reader(
            handle.master_fd, self._on_output, record.id)
        self._sweep_logs()  # a new file can tip the dir budgets over
        return record

    async def _cmd_kill(self, conn: _ClientConn, msg: dict) -> None:
        proc_id = msg.get("id")
        if self.handles.get(proc_id) is None or self.registry.get(proc_id) is None:
            await conn.send({"msg": "error",
                             "message": f"no such process: {proc_id!r}"})
            return
        # an explicit stop cancels a restart that is still waiting on the kill
        self._restart_after_exit.discard(proc_id)
        self._begin_kill(proc_id)

    def _begin_kill(self, proc_id: str) -> None:
        """Existing SIGINT -> SIGTERM -> SIGKILL escalation for a live handle."""
        handle = self.handles.get(proc_id)
        record = self.registry.get(proc_id)
        if handle is None or record is None:
            return
        record.state = STATE_STOPPING
        self._broadcast_snapshot()
        task = asyncio.ensure_future(
            asyncio.get_running_loop().run_in_executor(self._executor,
                                                       pty_host.kill, handle))
        self._kill_tasks.add(task)
        task.add_done_callback(self._kill_tasks.discard)

    async def _cmd_remove(self, conn: _ClientConn, msg: dict) -> None:
        """Drop the registry entry: exited directly; live only with explicit
        terminate-first intent, reusing the kill escalation and removing once
        the child exits (plain remove never touches a live process). An
        exited removal answers the requester with a notice naming the kept
        log file."""
        proc_id = msg.get("id")
        record = self.registry.get(proc_id)
        if record is None:
            await conn.send({"msg": "error",
                             "message": f"no such process: {proc_id!r}"})
            return
        if record.state == STATE_EXITED:
            # a protection restart may be waiting on its gap: cancel it first
            self._cancel_pending_restart(proc_id)
            self._remove_after_exit.discard(proc_id)
            self.registry.remove(proc_id)
            self._child_envs.pop(proc_id, None)
            self._broadcast_snapshot()
            # only this connection asked; other clients just see the snapshot
            message = f"removed '{record.name}'"
            if record.log_path and os.path.exists(record.log_path):
                message += f"; log kept: {record.log_path}"
            await conn.send({"msg": "notice", "message": message})
            return
        if msg.get("terminate_first") is not True:
            # only the literal JSON boolean true authorizes terminating a
            # live item: truthy strings/integers/null are rejected
            await conn.send({"msg": "error",
                             "message": f"cannot plain-remove {record.state} "
                                        f"process {proc_id!r}: terminate-first "
                                        "required"})
            return
        self._remove_after_exit.add(proc_id)
        # an explicit terminate-first remove cancels a queued restart
        self._restart_after_exit.discard(proc_id)
        if record.state == STATE_RUNNING:
            self._begin_kill(proc_id)
        # stopping: the kill already in flight will finish the removal

    async def _cmd_list(self, conn: _ClientConn, msg: dict) -> None:
        await conn.send(snapshot_msg(self.registry))

    async def _cmd_restart(self, conn: _ClientConn, msg: dict) -> None:
        proc_id = msg.get("id")
        record = self.registry.get(proc_id)
        if record is None:
            await conn.send({"msg": "error",
                             "message": f"no such process: {proc_id!r}"})
            return
        if record.state == STATE_EXITED:
            # a pending gap restart must not fire on top of this respawn
            self._cancel_pending_restart(proc_id)
            error = self._respawn(record)
            if error is not None:
                await conn.send({"msg": "error", "message": error})
            return
        self._restart_after_exit.add(proc_id)
        if record.state == STATE_RUNNING:
            self._begin_kill(proc_id)
        # stopping: the kill already in flight will hand over on exit

    def _cancel_pending_restart(self, proc_id: str) -> None:
        """Undo a scheduled protection restart: stop its timer and clear the
        row's pending flag (the record may already be gone)."""
        timer = self._pending_restarts.pop(proc_id, None)
        if timer is not None:
            timer.cancel()
        record = self.registry.get(proc_id)
        if record is not None:
            record.protect_pending = False

    async def _cmd_protect(self, conn: _ClientConn, msg: dict) -> None:
        """Set or clear the protection switch: limit None turns it off, -1 is
        unlimited, N >= 0 allows N consecutive quick restarts; gap is the
        seconds an auto restart waits before respawning. A valid protect
        cancels a restart still waiting on its gap and resets the quick
        counter; a rejected request changes nothing."""
        proc_id = msg.get("id")
        record = self.registry.get(proc_id)
        if record is None:
            await conn.send({"msg": "error",
                             "message": f"no such process: {proc_id!r}"})
            return
        limit = msg.get("limit")
        # bool is an int subclass: reject it explicitly, and reject anything
        # that is not None or an int >= -1
        if limit is not None and (isinstance(limit, bool)
                                  or not isinstance(limit, int)
                                  or limit < -1):
            await conn.send({"msg": "error",
                             "message": f"bad protect limit: {limit!r}"})
            return
        gap = msg.get("gap")
        if limit is not None:
            # bool is an int subclass, and json.loads accepts NaN/Infinity:
            # both must be rejected before the gap is taken as seconds
            if gap is None:
                gap = PROTECT_GAP_DEFAULT
            elif (isinstance(gap, bool) or not isinstance(gap, (int, float))
                  or not math.isfinite(gap) or gap < PROTECT_GAP_MIN):
                await conn.send({"msg": "error",
                                 "message": f"bad protect gap: {gap!r}"})
                return
        # only a fully valid protect may cancel a restart waiting on its gap
        self._cancel_pending_restart(proc_id)
        record.protect_limit = limit
        record.protect_gap = float(gap) if limit is not None else None
        record.quick_restarts = 0
        self._broadcast_snapshot()
        if limit is None:
            await conn.send({"msg": "notice",
                             "message": f"protection off for '{record.name}'"})
        else:
            label = "unlimited" if limit == -1 else str(limit)
            await conn.send({"msg": "notice",
                             "message": f"protection on for '{record.name}' "
                                        f"(max quick restarts: {label}, "
                                        f"restart gap: {gap:g}s)"})

    def _respawn(self, record: ProcessRecord, quick_restarts: int = 0) -> str | None:
        """Restart = remove + add: spawn a new row from the old record's
        launch info, then replace the old row in place so the new one keeps
        its position (the raw log file stays on disk). The new row inherits
        the protection switch and gap and carries the quick-restart count
        (manual restarts pass 0). Returns an error message when the spawn
        fails, None on success."""
        try:
            new = self._spawn_record(record.name, record.argv, record.cwd,
                                     self._child_envs.get(record.id))
        except OSError as exc:
            return f"restart failed: {exc}"
        # _spawn_record already stored the overlay under the new id
        self._child_envs.pop(record.id, None)
        new.protect_limit = record.protect_limit
        new.protect_gap = record.protect_gap
        new.quick_restarts = quick_restarts
        self.registry.replace(record.id, new.id)
        self._broadcast_snapshot()
        return None

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
            written = self._log_written.get(proc_id, 0) + len(chunk)
            if self.log_max_bytes and written > self.log_max_bytes:
                written = self._truncate_log(fh)
            self._log_written[proc_id] = written
        payload = base64.b64encode(chunk).decode()
        for conn in self.clients:
            if conn.subscribed == proc_id:
                conn.send_nowait({"msg": "log", "id": proc_id, "data": payload})

    def _truncate_log(self, fh) -> int:
        """Cap a raw log in place: rewrite its last half-budget, then sweep the
        dir. The fd stays open (same inode), so `tail -f` survives; the
        byte-level cut may split an escape sequence, like the ring buffer.
        Returns the new size so the caller resyncs its write counter."""
        fd = fh.fileno()
        size = os.fstat(fd).st_size  # authoritative; the counter only triggers
        if size <= self.log_max_bytes:  # shrank under us (operator rotated):
            return size  # just resync, nothing to cut
        keep = self.log_max_bytes // 2
        tail = os.pread(fd, keep, size - keep)  # pread leaves the write offset
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, tail)
        os.ftruncate(fd, len(tail))
        os.lseek(fd, 0, os.SEEK_END)
        self._sweep_logs()
        return len(tail)

    def _sweep_logs(self) -> None:
        """Drop the oldest logs of rows no longer on the dashboard until the
        dir is inside both budgets (bytes: factor * max_bytes; count:
        max_files). Logs of rows still on the dashboard, live or exited, are
        never candidates, so such a dir can stay over budget."""
        if not self.log_max_bytes:  # builtin off: logrotate owns the dir
            return
        if not (self.log_total_factor or self.log_max_files):
            return
        entries: list[tuple[float, int, Path]] = []
        try:
            for path in self.logs.glob("*.log"):
                try:
                    stat = path.stat()
                except OSError:
                    continue  # raced with an unlink
                entries.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            return
        total = sum(size for _, size, _ in entries)
        count = len(entries)
        budget = self.log_total_factor * self.log_max_bytes
        # a registry id means the row is still on the dashboard: keep its log
        listed = self.log_files.keys() | {rec.id for rec in self.registry.all()}
        dead = sorted((e for e in entries if e[2].stem not in listed),
                      key=lambda e: e[0])
        for _, size, path in dead:
            over_bytes = budget and total > budget
            over_count = self.log_max_files and count > self.log_max_files
            if not (over_bytes or over_count):
                break
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            total -= size
            count -= 1

    def _mark_exited(self, proc_id: str) -> None:
        handle = self.handles.pop(proc_id, None)
        record = self.registry.get(proc_id)
        # captured before the state flips: a tool-initiated end goes through
        # _begin_kill (state "stopping"), so still-running here means the
        # child exited on its own (crash, exit 0, `exit`, vim `:q`, ...)
        was_running = record is not None and record.state == STATE_RUNNING
        if handle is not None:
            self._detach_reader(handle)
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
        self._log_written.pop(proc_id, None)
        if proc_id in self._remove_after_exit:
            # terminate-first remove: exited now, so drop the entry (the raw
            # log file on disk stays) and let every client see it
            self._remove_after_exit.discard(proc_id)
            self.registry.remove(proc_id)
            self._child_envs.pop(proc_id, None)
        elif proc_id in self._restart_after_exit:
            # restart asked while the process was live: the kill has finished,
            # so respawn now; remove-after-exit wins over restart
            self._restart_after_exit.discard(proc_id)
            if record is not None:
                error = self._respawn(record)
                if error is not None:
                    for client in list(self.clients):
                        client.send_nowait({"msg": "error", "message": error})
        elif was_running and record.protect_limit is not None:
            # protection on and the child exited on its own: restart in place
            self._auto_restart(record)
        self._broadcast_snapshot()

    def _auto_restart(self, record: ProcessRecord) -> None:
        """Protection for a child that exited on its own: count the
        consecutive quick exits (run shorter than PROTECT_QUICK_EXIT); past
        the limit turn protection off and leave the row exited, otherwise
        mark it pending and schedule the respawn after the row's gap."""
        quick = (record.quick_restarts + 1
                 if time.time() - record.started_at < PROTECT_QUICK_EXIT
                 else 0)
        limit = record.protect_limit
        if limit != -1 and quick > limit:
            record.protect_limit = None
            record.protect_gap = None
            message = (f"'{record.name}' exited quickly {quick} times in a "
                       "row; protection turned off")
            for client in list(self.clients):
                client.send_nowait({"msg": "error", "message": message})
            return
        record.quick_restarts = quick
        record.protect_pending = True
        gap = record.protect_gap
        self._pending_restarts[record.id] = asyncio.get_running_loop().call_later(
            gap, self._fire_auto_restart, record.id)
        message = (f"'{record.name}' exited (code {record.exit_code}); "
                   f"protection restarts it in {gap:g}s")
        for client in list(self.clients):
            client.send_nowait({"msg": "notice", "message": message})

    def _fire_auto_restart(self, proc_id: str) -> None:
        """The gap elapsed: respawn unless the row was removed, restarted or
        unprotected in the meantime. The snapshot already shows the new row,
        so a success needs no extra notice."""
        self._pending_restarts.pop(proc_id, None)
        record = self.registry.get(proc_id)
        if (record is None or record.state != STATE_EXITED
                or record.protect_limit is None):
            return
        record.protect_pending = False
        error = self._respawn(record, quick_restarts=record.quick_restarts)
        if error is not None:
            for client in list(self.clients):
                client.send_nowait({"msg": "error", "message": error})

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
            rows, cols = struct.unpack(
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
                    if msg.get("msg") == "attached" and msg.get("id") == proc_id:
                        # Replay first: the command may have produced output
                        # before this takeover connection subscribed.
                        replay = msg.get("replay")
                        if replay:
                            os.write(out_fd, base64.b64decode(replay))
                    elif msg.get("msg") == "log" and msg.get("id") == proc_id:
                        os.write(out_fd, base64.b64decode(msg.get("data", "")))
    finally:
        if old_attrs is not None:
            try:
                termios.tcsetattr(in_fd, termios.TCSADRAIN, old_attrs)
            except (termios.error, OSError):
                pass  # operator terminal already gone (pump driven from a thread)
        # the child may leave a scroll region (watch's ESC[1;24r): reset it to
        # the full screen so Textual's "\n"-split repaint cannot scroll rows;
        # wrap in DECSC/DECRC because DECSTBM homes the cursor, which would
        # drop the shell prompt to the top line after `d` on the primary screen
        try:
            os.write(out_fd, b"\x1b7\x1b[r\x1b8")
        except OSError:
            pass  # operator terminal already gone
        # Drain unread daemon messages (snapshot broadcasts) before closing:
        # an RST would discard the last attach_input still in flight.
        try:
            sock.setblocking(False)
            while sock.recv(65536):
                pass
        except (BlockingIOError, OSError):
            pass
        sock.close()


def _close_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel whatever is still pending, bounded, then close the loop."""
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        try:
            loop.run_until_complete(asyncio.wait(pending, timeout=1.0))
        except RuntimeError:
            pass
    loop.close()


def run_daemon(name: str | None = None) -> int:
    socket_path, meta_path = deck_paths(name)
    sweep_stale_decks()
    # the override dir may be shared with other decks or hold other files:
    # never sweep it, only cap each file
    sweep_off = bool(os.environ.get("CLI_DECK_LOG_DIR"))
    daemon = DeckDaemon(socket_path, meta_path, deck_log_dir(name),
                        name=deck_name(name), hard_exit=True,
                        log_max_bytes=log_max_bytes(),
                        log_total_factor=0 if sweep_off else log_total_factor(),
                        log_max_files=0 if sweep_off else log_max_files())
    # No asyncio.run(): its teardown joins the default executor's threads, and
    # one parked kill thread there is what kept a stopped daemon alive.
    loop = asyncio.new_event_loop()
    status = 0
    try:
        loop.run_until_complete(daemon.run())
    except Exception:
        status = 1
        traceback.print_exc()  # stderr is DEVNULL for a spawned daemon
    finally:
        _close_loop(loop)
    # Leave the process directly: interpreter teardown joins non-daemon threads.
    os._exit(status)
