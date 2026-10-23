"""CLI detach, protection, row actions and deck listings."""
import asyncio
import contextlib
import io
import os
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from cli_deck.__main__ import (_build_parser, _print_recipe, _run_wrapper,
                               _split_wrapper, main)
from cli_deck.deckd import deck_paths, socket_alive
from tests import test_wrapper as wrapper


class CliFlagsTest(unittest.TestCase):
    def setUp(self):
        self.name = f"cli-flags-{uuid.uuid4().hex}"
        self.sock, _ = deck_paths(self.name)
        self.wrapper = wrapper.WrapperPrefixTest()
        self.addCleanup(self.wrapper._teardown, self.name)

    def _run_plain(self, args):
        return subprocess.run(
            [sys.executable, "-m", "cli_deck", *args],
            env=self.wrapper._env(self.name, None), capture_output=True,
            text=True, timeout=15)

    def test_detach_without_command_starts_daemon_and_prints_three_lines(self):
        proc = self._run_plain(["-d"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([line.split(":", 1)[0]
                          for line in proc.stdout.splitlines()],
                         ["deck", "socket", "attach"])
        self.assertIn(self.name, proc.stdout)
        self.assertIn(str(self.sock), proc.stdout)
        self.assertTrue(asyncio.run(socket_alive(self.sock)))

    def test_long_detach_prints_command_recipe(self):
        proc = self._run_plain(["--detach", "echo", "hi"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("command: echo hi", proc.stdout)
        self.assertIn("attach:", proc.stdout)

    def test_detach_rejects_resume(self):
        proc = self._run_plain(["-d", "-r"])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("error:", proc.stderr)

    def test_old_no_launch_flag_is_removed(self):
        proc = self._run_plain(["--no-launch", "true"])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("unrecognized arguments: --no-launch", proc.stderr)

    def test_protect_settings(self):
        for flag, settings in (("-p", (5, 5)), ("-p=5", (5, 5)),
                               ("-p=,2", (5, 2)),
                               ("--protect=-1,0.5", (-1, 0.5))):
            with self.subTest(flag=flag):
                opts, command = _split_wrapper([flag, "sleep", "300"])
                with contextlib.redirect_stderr(io.StringIO()):
                    try:
                        args = _build_parser().parse_args(opts)
                    except SystemExit as exc:
                        self.fail(f"{flag} rejected with exit {exc.code}")
                self.assertEqual(args.protect, settings)
                self.assertEqual(command, ["sleep", "300"])

    def test_invalid_protect_settings(self):
        for setting, message in (("abc", "not a number"),
                                  ("-2", "limit must be"),
                                  ("1,0.01", "gap must be"),
                                  ("1,2,3", "gap must be")):
            with self.subTest(setting=setting):
                proc = self._run_plain([f"-p={setting}", "sleep", "300"])
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn(message, proc.stderr)

    def test_protect_requires_command(self):
        proc = self._run_plain(["-p"])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("requires a command", proc.stderr)

    def test_protect_value_after_space_is_the_command(self):
        self.assertEqual(_split_wrapper(["-p", "5,2", "sleep", "300"]),
                         (["-p"], ["5,2", "sleep", "300"]))

    def test_detach_protect_sets_snapshot_and_recipe(self):
        proc = self._run_plain(["-d", "-p=3,1", "sleep", "300"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("protect: on (3, 1s)", proc.stdout)
        processes = asyncio.run(wrapper._deck_processes(self.sock))
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0]["protect_limit"], 3)
        self.assertEqual(processes[0]["protect_gap"], 1)

    def test_detach_recipe_protect_matches_list_format(self):
        for flag, line in (("-p=-1,1", "protect: on (∞, 1s)"),
                           ("-p=3,1", "protect: on (3, 1s)")):
            with self.subTest(flag=flag):
                protect = _build_parser().parse_args([flag]).protect
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    _print_recipe("sleep 300", "abc", {}, self.sock, protect)
                self.assertIn(line, stdout.getvalue().splitlines())

    def test_protect_error_is_reported(self):
        async def run():
            client = AsyncMock()
            client.messages = asyncio.Queue()
            for message in ({"msg": "snapshot", "processes": []},
                            {"msg": "added", "id": "abc"},
                            {"msg": "error", "message": "protect rejected"}):
                client.messages.put_nowait(message)
            stderr = io.StringIO()
            with patch("cli_deck.__main__.ensure_daemon", new=AsyncMock()), \
                    patch("cli_deck.__main__.DeckClient.connect",
                          new=AsyncMock(return_value=client)), \
                    contextlib.redirect_stderr(stderr):
                try:
                    rc = await _run_wrapper(["sleep", "300"], detach=True,
                                            protect=(3, 1))
                except TypeError as exc:
                    self.fail(str(exc))
            self.assertEqual(rc, 1)
            self.assertIn("protect rejected", stderr.getvalue())
            client.send.assert_any_await(
                {"cmd": "protect", "id": "abc", "limit": 3, "gap": 1})
            client.close.assert_awaited_once()
        asyncio.run(run())

    def _wait_processes(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            processes = asyncio.run(wrapper._deck_processes(self.sock))
            if predicate(processes):
                return processes
            time.sleep(0.05)
        self.fail(f"processes did not reach expected state: {processes}")

    def test_split_row_action_values_and_protect_command(self):
        for flag in ("--stop", "--restart", "--rm", "--up", "--unprotect"):
            with self.subTest(flag=flag):
                self.assertEqual(_split_wrapper([flag, "abc"]),
                                 ([flag, "abc"], []))
        self.assertEqual(_split_wrapper(["-d", "-p=5,2", "make", "w"]),
                         (["-d", "-p=5,2"], ["make", "w"]))

    def test_row_actions_require_id_and_are_mutually_exclusive(self):
        for flag in ("--stop", "--restart", "--rm", "--up", "--unprotect"):
            with self.subTest(flag=flag):
                proc = self._run_plain([flag])
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("expected one argument", proc.stderr)
        proc = self._run_plain(["--stop", "abc", "--rm", "abc"])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("not allowed with argument", proc.stderr)

    def test_row_actions_reject_other_modes_and_command(self):
        for suffix in (["echo", "hi"], ["-d"], ["-p"], ["-r"]):
            with self.subTest(suffix=suffix):
                proc = self._run_plain(["--stop", "abc", *suffix])
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("cannot be combined", proc.stderr)

    def test_row_action_without_deck(self):
        proc = self._run_plain(["--stop", "abc"])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("no running deck", proc.stderr)
        self.assertFalse(self.sock.exists())

    def test_row_action_timeout_is_reported(self):
        stderr = io.StringIO()
        with patch("cli_deck.__main__._run_row_action",
                   new=AsyncMock(side_effect=asyncio.TimeoutError())), \
                contextlib.redirect_stderr(stderr):
            rc = main(["--stop", "abc"])
        self.assertEqual(rc, 1)
        self.assertEqual(stderr.getvalue(), "cli-deck: row action timed out\n")

    def test_unknown_row_id_requires_exact_match(self):
        proc = self._run_plain(["-d", "sleep", "300"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc_id = self.wrapper._recipe_id(proc.stdout)
        for unknown in ("unknown", proc_id[:4]):
            with self.subTest(unknown=unknown):
                proc = self._run_plain(["--stop", unknown])
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("-l", proc.stderr)
        processes = asyncio.run(wrapper._deck_processes(self.sock))
        self.assertEqual(processes[0]["state"], "running")

    def test_protected_running_row_requires_unprotect_before_stop_remove(self):
        proc = self._run_plain(["-d", "-p=3,1", "sleep", "300"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc_id = self.wrapper._recipe_id(proc.stdout)
        for action in ("--stop", "--rm"):
            with self.subTest(action=action):
                proc = self._run_plain([action, proc_id])
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("is protected", proc.stderr)
                self.assertIn(f"cli-deck --up {proc_id}", proc.stderr)
                processes = asyncio.run(wrapper._deck_processes(self.sock))
                self.assertEqual(processes[0]["id"], proc_id)
                self.assertEqual(processes[0]["state"], "running")
                self.assertEqual(processes[0]["protect_limit"], 3)
        proc = self._run_plain(["--up", proc_id])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("protection off for", proc.stdout)
        processes = asyncio.run(wrapper._deck_processes(self.sock))
        self.assertIsNone(processes[0]["protect_limit"])
        proc = self._run_plain(["--rm", proc_id])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(f"cli-deck --stop {proc_id}", proc.stderr)
        proc = self._run_plain(["--stop", proc_id])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stopping", proc.stdout)
        self._wait_processes(lambda rows: rows[0]["state"] == "exited")
        proc = self._run_plain(["--stop", proc_id])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already exited", proc.stdout)
        proc = self._run_plain(["--rm", proc_id])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("log kept:", proc.stdout)
        self.assertIn(f"{proc_id}.log", proc.stdout)
        self.assertEqual(asyncio.run(wrapper._deck_processes(self.sock)), [])

    def test_pending_protected_exit_requires_unprotect_alias(self):
        proc = self._run_plain(["-d", "-p=3,30", "sleep", "0.5"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc_id = self.wrapper._recipe_id(proc.stdout)
        self._wait_processes(lambda rows: rows[0]["protect_pending"])
        for action in ("--stop", "--rm"):
            with self.subTest(action=action):
                proc = self._run_plain([action, proc_id])
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(f"cli-deck --up {proc_id}", proc.stderr)
                processes = asyncio.run(wrapper._deck_processes(self.sock))
                self.assertEqual(processes[0]["state"], "exited")
                self.assertTrue(processes[0]["protect_pending"])
        proc = self._run_plain(["--unprotect", proc_id])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("protection off for", proc.stdout)
        processes = asyncio.run(wrapper._deck_processes(self.sock))
        self.assertIsNone(processes[0]["protect_limit"])
        self.assertFalse(processes[0]["protect_pending"])
        proc = self._run_plain(["--rm", proc_id])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(asyncio.run(wrapper._deck_processes(self.sock)), [])

    def test_restart_protected_row_replaces_id_and_keeps_settings(self):
        proc = self._run_plain(["-d", "-p=3,1", "sleep", "300"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc_id = self.wrapper._recipe_id(proc.stdout)
        proc = self._run_plain(["--restart", proc_id])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        processes = self._wait_processes(
            lambda rows: len(rows) == 1 and rows[0]["id"] != proc_id)
        self.assertEqual(processes[0]["state"], "running")
        self.assertEqual(processes[0]["protect_limit"], 3)
        self.assertEqual(processes[0]["protect_gap"], 1)

    def test_row_action_fence_reports_notices_and_late_errors(self):
        for kind, expected_rc in (("notice", 0), ("error", 1)):
            with self.subTest(kind=kind):
                client = AsyncMock()
                client.messages = asyncio.Queue()
                for message in (
                        {"msg": "snapshot", "processes": [
                            {"id": "abc", "name": "sleep 300",
                             "state": "running", "protect_limit": None}]},
                        {"msg": "snapshot", "processes": []},
                        {"msg": "notice", "message": "action notice"},
                        {"msg": kind, "message": "action response"},
                        {"msg": "attached", "id": None}):
                    client.messages.put_nowait(message)
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("cli_deck.__main__.socket_alive",
                           new=AsyncMock(return_value=True)), \
                        patch("cli_deck.__main__.DeckClient.connect",
                              new=AsyncMock(return_value=client)), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    try:
                        rc = main(["--stop", "abc"])
                    except SystemExit as exc:
                        rc = exc.code
                self.assertEqual(rc, expected_rc, stderr.getvalue())
                self.assertIn("action notice", stdout.getvalue())
                self.assertIn("action response", stdout.getvalue()
                              if kind == "notice" else stderr.getvalue())
                self.assertEqual(client.send.await_args_list[-1].args,
                                 ({"cmd": "attach", "id": None},))
                client.close.assert_awaited_once()

    def test_remove_rejects_stopping_row(self):
        client = AsyncMock()
        client.messages = asyncio.Queue()
        client.messages.put_nowait({"msg": "snapshot", "processes": [
            {"id": "abc", "name": "sleep 300", "state": "stopping",
             "protect_limit": None}]})
        stderr = io.StringIO()
        with patch("cli_deck.__main__.socket_alive",
                   new=AsyncMock(return_value=True)), \
                patch("cli_deck.__main__.DeckClient.connect",
                      new=AsyncMock(return_value=client)), \
                contextlib.redirect_stderr(stderr):
            try:
                rc = main(["--rm", "abc"])
            except SystemExit as exc:
                rc = exc.code
        self.assertEqual(rc, 1, stderr.getvalue())
        self.assertIn("is stopping; stop it first", stderr.getvalue())
        client.send.assert_not_awaited()

    def test_list_shows_process_fields_with_name_last(self):
        proc = self._run_plain(["-d", "-p=3,1", "sleep", "300"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc_id = self.wrapper._recipe_id(proc.stdout)
        listing = self._run_plain(["-l"])
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("protect", listing.stdout)
        rows = [line for line in listing.stdout.splitlines() if proc_id in line]
        self.assertEqual(len(rows), 1, listing.stdout)
        self.assertTrue(rows[0].startswith("  "))
        self.assertIn("running", rows[0])
        self.assertIn("on (3, 1s)", rows[0])
        self.assertTrue(rows[0].endswith("sleep 300"), rows[0])

    def test_list_empty_deck(self):
        proc = self._run_plain(["-d"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        listing = self._run_plain(["-l"])
        self.assertEqual(listing.returncode, 0, listing.stderr)
        lines = listing.stdout.splitlines()
        deck_index = next(index for index, line in enumerate(lines)
                          if line.startswith(self.name))
        self.assertLess(deck_index + 1, len(lines), listing.stdout)
        self.assertEqual(lines[deck_index + 1], "  (no processes)")

    def test_list_continues_after_unreachable_and_timeout(self):
        hanging, empty = AsyncMock(), AsyncMock()
        hanging.messages = asyncio.Queue()
        empty.messages = asyncio.Queue()
        empty.messages.put_nowait({"msg": "snapshot", "processes": []})
        decks = [{"name": name, "socket": str(self.sock) + name,
                  "pid": os.getpid(), "started_at": index}
                 for index, name in enumerate(("missing", "hanging", "empty"))]
        stdout = io.StringIO()
        with patch("cli_deck.__main__.sweep_stale_decks"), \
                patch("cli_deck.__main__.list_decks", return_value=decks), \
                patch("cli_deck.__main__.DeckClient.connect",
                      new=AsyncMock(side_effect=[OSError("gone"), hanging, empty])), \
                contextlib.redirect_stdout(stdout):
            rc = main(["-l"])
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue().count("  (unreachable)"), 2)
        self.assertIn("  (no processes)", stdout.getvalue())
        hanging.close.assert_awaited_once()
        empty.close.assert_awaited_once()
