"""Textual TUI: list deck processes, add/kill, follow one process live."""
from __future__ import annotations

import asyncio
import base64
import math
import re
import sys
import time
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, Binding, ComposeResult, SuspendNotSupported
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Log, Static

from .deckd import (
    PROTECT_GAP_DEFAULT,
    PROTECT_GAP_MIN,
    PROTECT_LIMIT_DEFAULT,
    DeckClient,
    run_takeover,
    term_winsize,
)

# In-app guidance: terse but actionable, so the operator never has to guess
# what the detail screen is (read-only) or how to get full-screen control.
# The dashboard bar separates the two view modes from the other actions; the
# detail bar states the mode and how to reach/leave the interactive terminal.
DASHBOARD_HELP = (
    "VIEW  enter=read-only logs (keys NOT sent)   "
    "t=INTERACTIVE terminal, full-screen (Ctrl-] returns here)\n"
    "ACTIONS  a add  x stop/remove  r restart  p protect  d detach (keeps)  "
    "q stop daemon (kills)"
)
DETAIL_BAR = (
    "READ-ONLY: keys NOT sent to the process (esc reaches it)   "
    "t=interactive full-screen (Ctrl-] returns)   b=dashboard"
)

# Textual's Log only strips \x00-\x14, so ESC leaks to the real terminal:
# watch's ESC[1;24r wrecks the dashboard's scroll region and its repaint
# loses the header. Strip escape sequences from the read-only detail log;
# a sequence split across chunks degrades to dropping just the ESC
# (cosmetic only, no cross-chunk buffering).
ANSI_ESCAPE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"           # CSI ... final byte
    r"|\x1b[ -/]*[0-~]"                   # other ESC seqs (charset, keypad)
    r"|\x1b"                              # leftover lone ESC
)


class PromptScreen(ModalScreen):
    """Single-line text prompt; dismisses with the value or None on cancel."""

    CSS = """
    PromptScreen { align: center middle; }
    #prompt-box { width: 70%; min-width: 60; height: auto; padding: 1 2;
                  border: round thick; background: $panel; }
    #prompt-hint { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, title: str, placeholder: str = "") -> None:
        super().__init__()
        self.title = title
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            # markup=False: process names in the title render as plain text
            yield Static(self.title, markup=False)
            yield Input(placeholder=self.placeholder, id="prompt-input")
            yield Static("enter=submit  esc=cancel", id="prompt-hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def key_escape(self) -> None:
        self.dismiss(None)


def parse_protect_settings(limit_text: str, gap_text: str) -> tuple[int, float]:
    """Parse the two ProtectScreen fields; empty means the default. Raises
    ValueError with operator-facing messages on anything else."""
    limit_text = limit_text.strip()
    gap_text = gap_text.strip()
    if not limit_text:
        limit = PROTECT_LIMIT_DEFAULT
    else:
        try:
            limit = int(limit_text)
        except ValueError:
            raise ValueError(f"not a number: {limit_text}") from None
        if limit < -1:
            raise ValueError(f"limit must be -1 (unlimited) or 0+: {limit}")
    if not gap_text:
        gap = PROTECT_GAP_DEFAULT
    else:
        try:
            gap = float(gap_text)
        except ValueError:
            gap = float("nan")  # lands in the shared message below
        if not math.isfinite(gap) or gap < PROTECT_GAP_MIN:
            raise ValueError(
                f"gap must be a number of seconds >= {PROTECT_GAP_MIN:g}: "
                f"{gap_text}")
    return limit, gap


class ProtectScreen(ModalScreen):
    """Two-field protection prompt; dismisses with (limit, gap) or None on
    cancel. Invalid input notifies and keeps the dialog with both values."""

    CSS = """
    ProtectScreen { align: center middle; }
    #protect-box { width: 70%; min-width: 60; height: auto; padding: 1 2;
                   border: round thick; background: $panel; }
    #protect-hint { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="protect-box"):
            # markup=False: process names in the title render as plain text
            yield Static(self.title, markup=False)
            yield Static(f"Max quick restarts in a row (run < 10s), "
                         f"empty = {PROTECT_LIMIT_DEFAULT}, -1 = unlimited")
            yield Input(placeholder=str(PROTECT_LIMIT_DEFAULT),
                        id="protect-limit")
            yield Static(f"Restart gap in seconds, "
                         f"empty = {PROTECT_GAP_DEFAULT:g}, "
                         f"min {PROTECT_GAP_MIN:g}")
            yield Input(placeholder=f"{PROTECT_GAP_DEFAULT:g}",
                        id="protect-gap")
            yield Static("tab=next field  enter=submit  esc=cancel",
                         id="protect-hint")

    def on_mount(self) -> None:
        self.query_one("#protect-limit", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        limit_input = self.query_one("#protect-limit", Input)
        gap_input = self.query_one("#protect-gap", Input)
        try:
            settings = parse_protect_settings(limit_input.value,
                                              gap_input.value)
        except ValueError as exc:
            # keep the dialog and both values: the fix is one edit away
            self.app.notify(str(exc), severity="error")
            return
        self.dismiss(settings)

    def key_escape(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen):
    """Yes/no confirmation; dismisses with True/False. With default_no set
    (stop/restart, quit) No holds focus on open, so Enter cancels."""

    # auto-focus lands on the default button directly: a deferred focus()
    # in on_mount runs after the screen auto-focus, so Yes flashes first
    AUTO_FOCUS = ".default"

    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box { width: auto; max-width: 100%; height: auto; padding: 1 2;
                   border: round thick; background: $panel; }
    #confirm-question { width: auto; max-width: 100%; }
    #confirm-buttons { width: auto; min-width: 100%; height: auto;
                       margin-top: 1; align-horizontal: center; }
    """
    # the question's max-width: 100% keeps long text wrapping inside the box
    # content area instead of keeping an intrinsic width that overflows the
    # viewport-capped box; the button row keeps its intrinsic width (so the
    # group always contributes to the box's auto size) but never renders
    # narrower than the content area, and centers Stop+Cancel / Yes+No as
    # one group inside it

    def __init__(self, question: str, yes_label: str = "Stop",
                 no_label: str = "Cancel", default_no: bool = False) -> None:
        super().__init__()
        self.question = question
        self.yes_label = yes_label
        self.no_label = no_label
        # stop/restart and quit focus No so Enter cancels
        self.default_no = default_no

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            # markup=False: process names in the question render as plain text
            yield Static(self.question, id="confirm-question", markup=False)
            with Horizontal(id="confirm-buttons"):
                # AUTO_FOCUS matches ".default": the button Enter activates
                yes_default = "" if self.default_no else "default"
                no_default = "default" if self.default_no else ""
                yield Button(self.yes_label, variant="error", id="yes",
                             classes=yes_default)
                yield Button(self.no_label, variant="primary", id="no",
                             classes=no_default)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def key_left(self) -> None:
        self.query_one("#yes", Button).focus()

    def key_right(self) -> None:
        self.query_one("#no", Button).focus()

    def key_escape(self) -> None:
        self.dismiss(False)


class DetailScreen(Screen):
    """Read-only live follow + scrollback for one process (t = takeover)."""

    CSS = """
    DetailScreen { background: $background; }
    Log { width: 1fr; height: 1fr; }
    #detail-proc { height: 2; padding: 0 1; background: $panel;
                   color: $text; border-top: thick $primary; }
    #detail-bar { height: auto; padding: 0 1; background: $panel; color: $text; }
    """

    BINDINGS = [
        Binding("b", "leave", "back"),
        Binding("t", "takeover", "full-screen"),
    ]

    def __init__(self, proc_id: str, proc_name: str) -> None:
        super().__init__()
        self.proc_id = proc_id
        self.proc_name = proc_name

    def compose(self) -> ComposeResult:
        yield Log(id="detail-log", max_lines=10000, auto_scroll=True)
        # persistent bottom info/action area, below the log; the process name
        # is plain text (markup=False) so bracket-containing names render as-is
        yield Static(f"process: {self.proc_name}", id="detail-proc", markup=False)
        yield Static(DETAIL_BAR, id="detail-bar")

    def on_mount(self) -> None:
        self.app.current_detail = self
        self.query_one(Log).focus()
        self.app.client_send({"cmd": "attach", "id": self.proc_id})
        self.app.send_winsize(self.proc_id)

    def on_unmount(self) -> None:
        if self.app.current_detail is self:
            self.app.current_detail = None
        self.app.client_send({"cmd": "attach", "id": None})

    def feed(self, data: bytes) -> None:
        text = ANSI_ESCAPE.sub("", data.decode("utf-8", errors="replace"))
        if text:
            self.query_one(Log).write(text)

    def action_leave(self) -> None:
        self.app.pop_screen()

    def action_takeover(self) -> None:
        self.app.takeover_process(self.proc_id)

    def key_escape(self) -> None:
        """ESC stays the child program's key (vim and friends need it):
        forward it to the pty instead of using it for navigation."""
        self.app.client_send({"cmd": "attach_input", "id": self.proc_id,
                              "data": base64.b64encode(b"\x1b").decode()})


class DeckApp(App):
    """cli-deck control center."""

    TITLE = "cli-deck"

    CSS = """
    #proc-table { height: 1fr; }
    #help-bar { height: auto; padding: 0 1; background: $panel;
                color: $text; border-top: thick $primary; }
    """

    BINDINGS = [
        Binding("a", "add", "add"),
        Binding("t", "takeover", "full-screen"),
        Binding("x", "kill", "stop/remove"),
        Binding("r", "restart", "restart"),
        Binding("p", "protect", "protect"),
        Binding("d", "detach", "detach"),
        Binding("q", "stop_daemon", "stop daemon"),
    ]

    def __init__(self, client: DeckClient, socket_path: Path,
                 select_id: str | None = None) -> None:
        super().__init__()
        self.client = client
        self.socket_path = socket_path
        self.current_detail: DetailScreen | None = None
        self._outbox: asyncio.Queue = asyncio.Queue()
        # row the wrapper asked to select once (cli-deck <command>); cleared
        # after the first snapshot that contains it, so later updates never
        # move the user's cursor
        self._select_id = select_id
        # the table is only meaningfully empty after the first snapshot:
        # before it, q must still confirm (processes may exist but be unseen)
        self._has_snapshot = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield DataTable(id="proc-table", cursor_type="row")
        yield Static(DASHBOARD_HELP, id="help-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("id", "name", "state", "exit", "started", "protect")
        table.focus()
        self.run_worker(self._consume_messages())
        self.run_worker(self._drain_outbox())
        self.client_send({"cmd": "list"})

    # -- daemon I/O ---------------------------------------------------------

    def client_send(self, msg: dict) -> None:
        """Queue a command; one worker sends them in order."""
        self._outbox.put_nowait(msg)

    async def _drain_outbox(self) -> None:
        while True:
            msg = await self._outbox.get()
            try:
                await self.client.send(msg)
            except (ConnectionError, OSError):
                self.notify("lost connection to the deck daemon", severity="error")

    def send_winsize(self, proc_id: str) -> None:
        rows, cols = term_winsize(sys.stdout.fileno())
        self.client_send({"cmd": "winsize", "id": proc_id, "rows": rows, "cols": cols})

    def on_resize(self, event: events.Resize) -> None:
        if self.current_detail is not None:
            self.send_winsize(self.current_detail.proc_id)

    async def _consume_messages(self) -> None:
        while True:
            msg = await self.client.messages.get()
            self.handle_daemon_message(msg)

    def handle_daemon_message(self, msg: dict) -> None:
        kind = msg.get("msg")
        if kind == "snapshot":
            self._has_snapshot = True
            self.update_table(msg.get("processes", []))
        elif kind == "log":
            detail = self.current_detail
            if detail is not None and detail.proc_id == msg.get("id"):
                detail.feed(base64.b64decode(msg.get("data", "")))
        elif kind == "attached":
            detail = self.current_detail
            if detail is not None and detail.proc_id == msg.get("id"):
                replay = msg.get("replay")
                if replay:
                    detail.feed(base64.b64decode(replay))
        elif kind == "notice":
            # names and paths may contain brackets: plain text, never markup
            self.notify(msg.get("message", ""), markup=False)
        elif kind == "error":
            self.notify(msg.get("message", ""), severity="error")

    def update_table(self, processes: list[dict]) -> None:
        """Update rows in place by key; rows follow the snapshot (registry)
        order and the cursor stays on the same row across updates."""
        table = self.query_one(DataTable)
        order = {p["id"]: index for index, p in enumerate(processes)}
        wanted = {p["id"]: self._row_cells(p) for p in processes}
        # remember the cursor's row before the edit so it can follow it
        cursor_row = table.cursor_row
        cursor_id = (str(table.get_row_at(cursor_row)[0])
                     if table.row_count else None)
        for row_key in list(table.rows.keys()):
            if row_key.value not in wanted:
                table.remove_row(row_key)
        for row_key in list(table.rows.keys()):
            cells = wanted.pop(row_key.value, None)
            if cells is None:
                continue
            row_index = table.get_row_index(row_key)
            for col, value in enumerate(cells):
                # update_width: in-place updates must also widen a column
                # (add_row alone does not keep the old cells from clipping)
                table.update_cell_at((row_index, col), value,
                                     update_width=True)
        for proc_id, cells in wanted.items():
            table.add_row(*cells, key=proc_id)
        # follow the snapshot order (a restart replaces its row in place);
        # sort keeps the cursor index, so reposition the cursor afterwards
        table.sort(key=lambda cells: order[str(cells[0])])
        if cursor_id is not None and table.row_count > 0:
            if cursor_id in table.rows:
                table.move_cursor(row=table.get_row_index(cursor_id))
            else:
                table.move_cursor(row=min(cursor_row, table.row_count - 1))
        self._apply_pending_selection(table)

    def _apply_pending_selection(self, table: DataTable) -> None:
        """Move the cursor to the wrapper's new process once; after that the
        user's cursor choice survives every snapshot update."""
        if self._select_id is None:
            return
        for row_key in table.rows:
            if row_key.value == self._select_id:
                table.move_cursor(row=table.get_row_index(row_key))
                self._select_id = None
                return

    @staticmethod
    def _row_cells(p: dict) -> tuple[str, ...]:
        started = time.strftime("%H:%M:%S", time.localtime(p.get("started_at", 0)))
        exit_code = "" if p.get("exit_code") is None else str(p["exit_code"])
        limit = p.get("protect_limit")  # absent in old snapshots: shows ""
        gap = p.get("protect_gap")
        if limit is None:
            protect = ""
        elif p.get("protect_pending"):
            protect = f"restart in {gap:g}s"  # static text, no countdown
        elif gap is None:  # old daemon without protect_gap
            protect = "on (∞)" if limit == -1 else f"on ({limit})"
        else:
            label = "∞" if limit == -1 else str(limit)
            protect = f"on ({label}, {gap:g}s)"
        return (p["id"], p["name"], p["state"], exit_code, started, protect)

    # -- actions ------------------------------------------------------------

    def _selected_row(self) -> list[str] | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        return table.get_row_at(table.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_detail()  # enter on a focused row

    def action_add(self) -> None:
        def done(value: str | None) -> None:
            if value is None:
                return
            self.client_send({"cmd": "add", "command": value.strip() or None})

        self.push_screen(
            PromptScreen("Command to launch (empty = plain bash -i)",
                         "e.g. ssh prod  /  my-alias"),
            done,
        )

    def action_detail(self) -> None:
        row = self._selected_row()
        if row is None:
            self.notify("no process selected")
            return
        self.push_screen(DetailScreen(str(row[0]), str(row[1])))

    def action_takeover(self) -> None:
        row = self._selected_row()
        if row is None:
            self.notify("no process selected")
            return
        self.takeover_process(str(row[0]))

    def takeover_process(self, proc_id: str) -> None:
        """Hand the real terminal to the process pty until Ctrl-]."""
        try:
            with self.suspend():
                run_takeover(self.socket_path, proc_id,
                             sys.stdin.fileno(), sys.stdout.fileno())
        except SuspendNotSupported:
            self.notify("takeover needs a real terminal", severity="error")
            return
        self.client_send({"cmd": "list"})

    def action_kill(self) -> None:
        """Dashboard-only: never touch a hidden row behind detail/modals."""
        if self.current_detail is not None or isinstance(self.screen,
                                                         ModalScreen):
            return
        row = self._selected_row()
        if row is None:
            self.notify("no process selected")
            return
        proc_id, name, state = str(row[0]), str(row[1]), str(row[2])
        if state == "stopping":
            self.client_send({"cmd": "kill", "id": proc_id})
            return
        if state == "exited":
            self.client_send({"cmd": "remove", "id": proc_id,
                              "terminate_first": False})
            return
        # capture the id now: cursor moves or snapshot churn while the
        # confirm is open cannot retarget Yes to another process
        def done(confirmed: bool) -> None:
            if confirmed:
                self.client_send({"cmd": "kill", "id": proc_id})

        self.push_screen(
            ConfirmScreen(f"Stop '{self._display_name(name)}'?",
                          yes_label="Yes", no_label="No", default_no=True),
            done,
        )

    @staticmethod
    def _display_name(name: str) -> str:
        """Shorten only the display name so the consequence and both buttons
        always stay visible on narrow screens; budget by terminal display
        cells, not codepoints (CJK is double-width)."""
        text = Text(name)
        text.truncate(60, overflow="ellipsis")
        return text.plain

    def action_restart(self) -> None:
        """Dashboard-only: never restart a hidden row behind detail/modals."""
        if self.current_detail is not None or isinstance(self.screen,
                                                         ModalScreen):
            return
        row = self._selected_row()
        if row is None:
            self.notify("no process selected")
            return
        proc_id, name, state = str(row[0]), str(row[1]), str(row[2])
        if state != "running":
            self.client_send({"cmd": "restart", "id": proc_id})
            return
        # capture the id now: cursor moves or snapshot churn while the
        # confirm is open cannot retarget Yes to another process
        def done(confirmed: bool) -> None:
            if confirmed:
                self.client_send({"cmd": "restart", "id": proc_id})

        self.push_screen(
            ConfirmScreen(f"Restart '{self._display_name(name)}'? "
                          "Yes stops it first.",
                          yes_label="Yes", no_label="No", default_no=True),
            done,
        )

    def action_protect(self) -> None:
        """Dashboard-only: never touch a hidden row behind detail/modals."""
        if self.current_detail is not None or isinstance(self.screen,
                                                         ModalScreen):
            return
        row = self._selected_row()
        if row is None:
            self.notify("no process selected")
            return
        proc_id, name = str(row[0]), str(row[1])
        if str(row[5]):
            # already protected (a pending gap counts): p turns it off directly
            self.client_send({"cmd": "protect", "id": proc_id, "limit": None})
            return
        # capture the id now: cursor moves or snapshot churn while the
        # dialog is open cannot retarget the settings to another process
        def done(settings: tuple[int, float] | None) -> None:
            if settings is None:
                return
            limit, gap = settings
            self.client_send({"cmd": "protect", "id": proc_id,
                              "limit": limit, "gap": gap})

        self.push_screen(
            ProtectScreen(f"Protect '{self._display_name(name)}': "
                          "auto-restart when it exits on its own."),
            done,
        )

    def action_detach(self) -> None:
        self.exit()  # daemon keeps running

    def action_stop_daemon(self) -> None:
        # no entries means nothing gets killed, so q leaves right away;
        # before the first snapshot the empty table is not proof of that
        if self._has_snapshot and self.query_one(DataTable).row_count == 0:
            self.run_worker(self._stop_and_exit())
            return

        def done(confirmed: bool) -> None:
            if confirmed:
                self.run_worker(self._stop_and_exit())

        self.push_screen(
            ConfirmScreen("Stop the daemon and kill all its processes?",
                          default_no=True), done)

    async def _stop_and_exit(self) -> None:
        try:
            await self.client.send({"cmd": "stop"})
        except (ConnectionError, OSError):
            pass
        await asyncio.sleep(0.2)  # let the daemon tear down
        self.exit()
