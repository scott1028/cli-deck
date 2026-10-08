import os
import unittest

from cli_deck import pty_host


class PtyHostTest(unittest.TestCase):
    def test_spawn_bash_ic_echo(self):
        handle = pty_host.spawn("t-echo", ["bash", "-ic", "echo hello-from-pty"])
        try:
            out, code = pty_host.read_until_exit(handle)
        finally:
            pty_host.close(handle)
        self.assertIn(b"hello-from-pty", out)
        self.assertEqual(code, 0)

    def test_winsize_applies(self):
        handle = pty_host.spawn("t-size", ["bash", "-ic", "stty size"],
                                rows=40, cols=120)
        try:
            out, _ = pty_host.read_until_exit(handle)
        finally:
            pty_host.close(handle)
        self.assertIn(b"40 120", out)

    def test_set_winsize_midflight(self):
        handle = pty_host.spawn("t-resize",
                                ["bash", "-ic", "sleep 1; stty size"])
        try:
            pty_host.set_winsize(handle, 33, 99)
            out, _ = pty_host.read_until_exit(handle)
        finally:
            pty_host.close(handle)
        self.assertIn(b"33 99", out)

    def test_kill_escalation_stops_sleep(self):
        # exec keeps sleep in the launched process group, so killpg reaches it.
        handle = pty_host.spawn("t-sleep", ["bash", "-ic", "exec sleep 120"])
        try:
            code = pty_host.kill(handle)
            self.assertIsNotNone(code)
            self.assertIsNotNone(handle.poll())
            with self.assertRaises(ProcessLookupError):
                os.killpg(handle.proc.pid, 0)
        finally:
            pty_host.close(handle)


if __name__ == "__main__":
    unittest.main()
