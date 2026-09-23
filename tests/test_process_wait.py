from __future__ import annotations

import contextlib
import errno
import io
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest
from unittest import mock

from backends.deltabox.gsd import process_wait, sandbox_controller


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class FakePoll:
    def __init__(self, clock, events=(), register_error=None, poll_error=None):
        self.clock = clock
        self.events = list(events)
        self.registered = {}
        self.waits = []
        self.register_error = register_error
        self.poll_error = poll_error

    def register(self, fd, mask):
        if self.register_error is not None:
            raise self.register_error
        self.registered[fd] = mask

    def unregister(self, fd):
        del self.registered[fd]

    def poll(self, timeout):
        self.waits.append(timeout)
        if self.poll_error is not None:
            error, self.poll_error = self.poll_error, None
            self.clock.now += 0.002
            raise error
        end = self.clock.now + timeout / 1000
        active = [(at, fd, mask) for at, fd, mask in self.events
                  if fd in self.registered]
        if active:
            at, fd, mask = min(active)
            if at <= end:
                self.clock.now = max(self.clock.now, at)
                self.events.remove((at, fd, mask))
                return [(fd, mask)]
        self.clock.now = end
        return []


class ProcessWaitTest(unittest.TestCase):
    def setUp(self):
        self.module = process_wait
        self.clock = FakeClock()
        self.poller = FakePoll(self.clock)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(
            self.module.time, "monotonic", self.clock.monotonic))
        self.stack.enter_context(mock.patch.object(
            self.module.time, "sleep", self.clock.sleep))
        self.open_pidfd = self.stack.enter_context(mock.patch.object(
            self.module.os, "pidfd_open", side_effect=lambda pid: pid + 100,
            create=True))
        self.close_fd = self.stack.enter_context(mock.patch.object(
            self.module.os, "close"))
        self.stack.enter_context(mock.patch.object(
            self.module.select, "poll", return_value=self.poller, create=True))

    def wait(self, pids, timeout=1.0, is_done=lambda pid: False):
        return self.module.wait_for_process_exit(pids, timeout, is_done)

    def assertClosed(self, *fds):
        self.assertCountEqual([call.args[0] for call in self.close_fd.call_args_list], fds)

    def test_empty_and_already_done_do_not_open_descriptors(self):
        self.assertEqual(self.wait([]), set())
        self.assertEqual(self.wait([1, 1, 2], is_done=lambda pid: True), set())
        self.open_pidfd.assert_not_called()
        self.assertClosed()

    def test_initial_probe_runs_even_with_no_time_left(self):
        self.assertEqual(self.wait([1, 2], timeout=0,
                                   is_done=lambda pid: pid == 1), {2})
        self.open_pidfd.assert_not_called()

    def test_ready_pidfd_returns_without_ten_millisecond_sleep(self):
        self.poller.events = [(0.003, 101, select.POLLIN)]
        done = mock.Mock(return_value=False)
        self.assertEqual(self.wait([1, 1], is_done=done), set())
        self.assertLess(self.clock.now, 0.01)
        done.assert_called_once_with(1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertClosed(101)

    def test_multiple_exits_and_hangup(self):
        self.poller.events = [(0.002, 101, select.POLLIN),
                              (0.005, 102, select.POLLHUP)]
        self.assertEqual(self.wait([1, 2]), set())
        self.assertAlmostEqual(self.clock.now, 0.005)
        self.assertClosed(101, 102)

    def test_esrch_between_probe_and_open_is_done(self):
        self.open_pidfd.side_effect = ProcessLookupError(errno.ESRCH, "gone")
        self.assertEqual(self.wait([1]), set())
        self.assertClosed()

    def test_unsupported_and_permission_errors_use_fallback(self):
        for error in (errno.ENOSYS, errno.EINVAL, errno.EPERM, errno.EACCES,
                      errno.EMFILE):
            with self.subTest(error=error):
                self.clock.now = 0
                self.clock.sleeps.clear()
                self.open_pidfd.side_effect = OSError(error, "pidfd unavailable")
                self.assertEqual(self.wait([1], is_done=lambda pid: self.clock.now >= 0.02), set())
                self.assertEqual(self.clock.sleeps, [0.01, 0.01])
        self.assertClosed()

    def test_missing_pidfd_api_uses_fallback(self):
        with mock.patch.object(self.module.os, "pidfd_open", None), \
                mock.patch.object(self.module, "_libc_pidfd_opener", return_value=None):
            self.assertEqual(self.wait([1], is_done=lambda pid: self.clock.now >= 0.01), set())
        self.assertEqual(self.clock.sleeps, [0.01])
        self.assertClosed()

    def test_missing_python_api_uses_compat_opener(self):
        self.poller.events = [(0.003, 101, select.POLLIN)]
        compat = mock.Mock(return_value=101)
        with mock.patch.object(self.module.os, "pidfd_open", None), \
                mock.patch.object(self.module, "_libc_pidfd_opener", return_value=compat):
            self.assertEqual(self.wait([1]), set())
        compat.assert_called_once_with(1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertClosed(101)

    def test_missing_poll_api_uses_fallback_without_opening_fds(self):
        with mock.patch.object(self.module.select, "poll", None):
            self.assertEqual(self.wait([1], is_done=lambda pid: self.clock.now >= 0.01), set())
        self.assertEqual(self.clock.sleeps, [0.01])
        self.open_pidfd.assert_not_called()

    def test_poll_creation_error_uses_fallback_without_opening_fds(self):
        with mock.patch.object(self.module.select, "poll",
                               side_effect=OSError(errno.ENOSYS, "unavailable")):
            self.assertEqual(self.wait([1], is_done=lambda pid: self.clock.now >= 0.01), set())
        self.assertEqual(self.clock.sleeps, [0.01])
        self.open_pidfd.assert_not_called()

    def test_mixed_pidfds_and_fallback(self):
        def open_pidfd(pid):
            if pid == 2:
                raise PermissionError(errno.EPERM, "denied")
            return 101

        self.open_pidfd.side_effect = open_pidfd
        self.poller.events = [(0.003, 101, select.POLLIN)]
        self.assertEqual(self.wait([1, 2], is_done=lambda pid: pid == 2 and self.clock.now >= 0.01), set())
        self.assertLessEqual(self.clock.now, 0.02)
        self.assertClosed(101)

    def test_zombie_leader_detected_even_without_pidfd_readiness(self):
        self.assertEqual(self.wait([1], is_done=lambda pid: self.clock.now >= 0.003), set())
        self.assertAlmostEqual(self.clock.now, 0.01)
        self.assertClosed(101)

    def test_other_ready_pids_do_not_delay_zombie_probe(self):
        self.poller.events = [(at, fd, select.POLLIN) for at, fd in
                              [(0.002, 101), (0.004, 102), (0.006, 103),
                               (0.008, 104), (0.009, 105)]]
        self.assertEqual(self.wait(range(1, 7),
                                   is_done=lambda pid: pid == 6 and self.clock.now >= 0.003), set())
        self.assertLess(self.clock.now, 0.012)
        self.assertClosed(*range(101, 107))

    def test_readiness_at_deadline_does_not_skip_final_zombie_probe(self):
        self.poller.events = [(0.005, 101, select.POLLIN)]
        self.assertEqual(self.wait([1, 2], timeout=0.005,
                                   is_done=lambda pid: pid == 2 and self.clock.now >= 0.003), set())
        self.assertClosed(101, 102)

    def test_timeout_returns_only_unresolved_and_closes_all_fds(self):
        self.poller.events = [(0.002, 101, select.POLLIN)]
        self.assertEqual(self.wait([1, 2], timeout=0.025), {2})
        self.assertGreaterEqual(self.clock.now, 0.025)
        self.assertLess(self.clock.now, 0.027)
        self.assertClosed(101, 102)

    def test_fallback_sleep_is_capped_by_absolute_deadline(self):
        self.open_pidfd.side_effect = OSError(errno.ENOSYS, "unsupported")
        self.assertEqual(self.wait([1], timeout=0.015), {1})
        self.assertAlmostEqual(self.clock.now, 0.015)
        self.assertAlmostEqual(self.clock.sleeps[-1], 0.005)

    def test_interrupted_poll_keeps_original_deadline(self):
        self.poller.poll_error = InterruptedError(errno.EINTR, "signal")
        self.assertEqual(self.wait([1], timeout=0.025), {1})
        self.assertGreaterEqual(self.clock.now, 0.025)
        self.assertLess(self.clock.now, 0.027)
        self.assertClosed(101)

    def test_repeated_interrupts_do_not_delay_zombie_or_fallback_probes(self):
        def open_pidfd(pid):
            if pid == 2:
                raise PermissionError(errno.EPERM, "denied")
            return 101

        def interrupted_poll(timeout):
            self.clock.now += 0.002
            raise InterruptedError(errno.EINTR, "signal")

        self.open_pidfd.side_effect = open_pidfd
        with mock.patch.object(self.poller, "poll", interrupted_poll):
            self.assertEqual(self.wait([1, 2], timeout=0.05,
                                       is_done=lambda pid: self.clock.now >= 0.003), set())
        self.assertLessEqual(self.clock.now, 0.012)
        self.assertClosed(101)

    def test_registration_error_falls_back_and_closes_fd(self):
        self.poller.register_error = OSError(errno.EPERM, "denied")
        self.assertEqual(self.wait([1], is_done=lambda pid: self.clock.now >= 0.01), set())
        self.assertEqual(self.clock.sleeps, [0.01])
        self.assertClosed(101)

    def test_unexpected_registration_exception_still_closes_fd(self):
        register = self.poller.register

        def register_second_fails(fd, mask):
            if self.poller.registered:
                raise RuntimeError("registration failed")
            register(fd, mask)

        with mock.patch.object(self.poller, "register", register_second_fails):
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                self.wait([1, 2])
        self.assertClosed(101, 102)

    def test_unexpected_open_exception_closes_previous_descriptors(self):
        self.open_pidfd.side_effect = [101, RuntimeError("open failed")]
        with self.assertRaisesRegex(RuntimeError, "open failed"):
            self.wait([1, 2])
        self.assertClosed(101)

    def test_poll_error_falls_back_to_predicate(self):
        self.poller.poll_error = OSError(errno.EINVAL, "unavailable")
        self.assertEqual(self.wait([1], is_done=lambda pid: self.clock.now >= 0.01), set())
        self.assertLessEqual(self.clock.now, 0.02)
        self.assertClosed(101)

    def test_invalid_fd_readiness_does_not_count_as_exit(self):
        self.poller.events = [(0.003, 101, select.POLLNVAL)]
        self.assertEqual(self.wait([1], timeout=0.025), {1})
        self.assertGreaterEqual(self.clock.now, 0.025)
        self.assertClosed(101)

    def test_predicate_exception_closes_open_descriptors(self):
        def is_done(pid):
            if self.clock.now:
                raise RuntimeError("probe failed")
            return False

        with self.assertRaisesRegex(RuntimeError, "probe failed"):
            self.wait([1, 2], is_done=is_done)
        self.assertClosed(101, 102)


class ControllerProcessWaitTest(unittest.TestCase):
    def setUp(self):
        self.controller = sandbox_controller.SandboxController.__new__(
            sandbox_controller.SandboxController)

    def test_proc_stat_comm_with_spaces_and_parentheses(self):
        for name in ("simple", "has spaces", "nested (inner) end", "trailing)"):
            for state in ("Z", "S"):
                with self.subTest(name=name, state=state):
                    content = f"123 ({name}) {state} 1 2 3 4\n"
                    with mock.patch("builtins.open", mock.mock_open(read_data=content)):
                        self.assertEqual(self.controller._pid_gone_or_zombie(123), state == "Z")

    def test_missing_proc_stat_is_done(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertTrue(self.controller._pid_gone_or_zombie(123))

    def test_incomplete_proc_stat_does_not_report_exit(self):
        for content in ("", "   \n", "123", "123 (", "123 (unfinished Z",
                        "123 (name)", "123 (name) ", "123 name Z 1 2 3"):
            with self.subTest(content=content):
                with mock.patch("builtins.open", mock.mock_open(read_data=content)):
                    self.assertFalse(self.controller._pid_gone_or_zombie(123))

    def test_timeout_warning_and_return_value_are_unchanged(self):
        with mock.patch.object(sandbox_controller, "wait_for_process_exit",
                               create=True, return_value={10, 2}) as wait:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = self.controller._wait_pids_gone_or_zombie([10, 2], timeout=0)
        self.assertIsNone(result)
        wait.assert_called_once_with([10, 2], 0, self.controller._pid_gone_or_zombie)
        self.assertEqual(output.getvalue(),
                         "[Warning] timed out waiting for killed pids to exit: [2, 10]\n")

    def test_guest_top_level_import(self):
        gsd_dir = Path(sandbox_controller.__file__).parent
        subprocess.run([sys.executable, "-c", "import sandbox_controller"],
                       cwd=gsd_dir, check=True, capture_output=True, text=True)


@unittest.skipUnless(sys.platform == "linux",
                     "requires Linux pidfd support")
class LinuxProcessWaitTest(unittest.TestCase):
    def setUp(self):
        try:
            opener = process_wait._pidfd_opener()
            if opener is None:
                self.skipTest("no supported pidfd opener")
            fd = opener(os.getpid())
        except OSError as error:
            self.skipTest(f"pidfd_open unavailable: {error}")
        else:
            os.close(fd)
        self.wait = process_wait.wait_for_process_exit
        controller = sandbox_controller.SandboxController.__new__(
            sandbox_controller.SandboxController)
        self.is_done = controller._pid_gone_or_zombie

    def child(self, delay):
        child = subprocess.Popen([sys.executable, "-c",
                                  f"import time; time.sleep({delay})"])

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait()

        self.addCleanup(cleanup)
        return child

    def test_multiple_real_exits_are_detected_before_reaping(self):
        children = [self.child(0.01), self.child(0.03)]
        self.assertEqual(self.wait([child.pid for child in children], 2.0, self.is_done), set())
        self.assertTrue(all(self.is_done(child.pid) for child in children))

    def test_real_running_process_times_out_without_being_signalled(self):
        child = self.child(5)
        start = time.monotonic()
        self.assertEqual(self.wait([child.pid], 0.025, self.is_done), {child.pid})
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.025)
        self.assertLess(elapsed, 1.0)
        self.assertIsNone(child.poll())

    def test_already_reaped_process_is_done(self):
        child = self.child(0)
        child.wait(timeout=2)
        self.assertEqual(self.wait([child.pid], 1.0, self.is_done), set())

    def test_real_exit_without_python_pidfd_api(self):
        child = self.child(0.02)
        with mock.patch.object(process_wait.os, "pidfd_open", None, create=True), \
                mock.patch.object(process_wait.time, "sleep", side_effect=AssertionError("poll fallback")):
            self.assertEqual(self.wait([child.pid], 2.0, self.is_done), set())
        self.assertTrue(self.is_done(child.pid))

    def test_compat_errno_propagates(self):
        opener = process_wait._libc_pidfd_opener()
        if opener is None:
            self.skipTest("no libc compatibility opener")
        with self.assertRaises(OSError) as caught:
            opener(-1)
        self.assertEqual(caught.exception.errno, errno.EINVAL)


if __name__ == "__main__":
    unittest.main()
