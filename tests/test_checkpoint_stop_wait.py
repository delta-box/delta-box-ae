"""Linux stop confirmation and deadline checks for the checkpoint wait path."""
import os
import signal
import sys
import time
import unittest
from unittest.mock import patch

from backends.deltabox.gsd import sandbox_controller as sc


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux procfs")
class CheckpointStopWaitTests(unittest.TestCase):
    def setUp(self):
        self.children = []
        self.controller = sc.SandboxController.__new__(sc.SandboxController)

    def tearDown(self):
        for pid, write_fd in self.children:
            os.close(write_fd)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    def child_waiting_for_command(self):
        read_fd, write_fd = os.pipe()
        ready_read, ready_write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(write_fd)
            os.close(ready_read)
            os.write(ready_write, b"r")
            os.close(ready_write)
            command = os.read(read_fd, 1)
            os.close(read_fd)
            if command == b"s":
                os.kill(os.getpid(), signal.SIGSTOP)
            os._exit(0)
        os.close(read_fd)
        os.close(ready_write)
        self.children.append((pid, write_fd))
        self.assertEqual(os.read(ready_read, 1), b"r")
        os.close(ready_read)
        return pid, write_fd

    def test_running_child_is_rejected_until_actual_stop(self):
        pid, write_fd = self.child_waiting_for_command()
        self.assertFalse(self.controller._wait_pid_stopped(pid, timeout=0.003))
        os.write(write_fd, b"s")
        self.assertTrue(self.controller._wait_pid_stopped(pid, timeout=0.2))
        stopped_pid, status = os.waitpid(pid, os.WUNTRACED | os.WNOHANG)
        self.assertEqual(stopped_pid, pid)
        self.assertTrue(os.WIFSTOPPED(status))
        self.assertEqual(os.WSTOPSIG(status), signal.SIGSTOP)

    def test_running_child_times_out_without_wall_clock_or_busy_spin(self):
        pid, _ = self.child_waiting_for_command()
        monotonic, real_sleep = time.monotonic, time.sleep
        sleeps = []

        def sleeping(delay):
            sleeps.append(delay)
            real_sleep(delay)

        started = monotonic()
        with patch.object(sc.time, "time", side_effect=AssertionError("wall clock used")), \
                patch.object(sc.time, "sleep", side_effect=sleeping):
            self.assertFalse(self.controller._wait_pid_stopped(pid, timeout=0.01))
        elapsed = monotonic() - started
        self.assertGreaterEqual(elapsed, 0.01)
        self.assertLess(elapsed, 0.2)
        self.assertTrue(sleeps)
        self.assertTrue(all(0 < delay <= 0.0001 for delay in sleeps))

    def test_exited_child_cannot_be_confirmed_stopped(self):
        pid, write_fd = self.child_waiting_for_command()
        os.write(write_fd, b"x")
        os.waitpid(pid, 0)
        self.assertFalse(self.controller._wait_pid_stopped(pid, timeout=0.01))

    def test_zero_timeout_does_not_confirm_running_child(self):
        pid, _ = self.child_waiting_for_command()
        self.assertFalse(self.controller._wait_pid_stopped(pid, timeout=0))


if __name__ == "__main__":
    unittest.main()
