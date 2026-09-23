"""Restore preparation never bypasses signalling or the exit barrier."""
import signal
import unittest
from unittest.mock import patch

from backends.deltabox.gsd import sandbox_controller as sc


class RestorePrepareTests(unittest.TestCase):
    def setUp(self):
        self.controller = sc.SandboxController.__new__(sc.SandboxController)
        self.events = []
        self.controller._wait_pids_gone_or_zombie = lambda pids: self.events.append(('wait', pids))

    def run_kill(self, pids, prepare=None):
        with patch.object(sc.os, 'kill', side_effect=lambda pid, sig: self.events.append(('kill', pid, sig))), \
             patch.object(sc.os, 'waitpid', side_effect=ChildProcessError):
            self.controller._kill_pids_and_wait(pids, 'restore', prepare)

    def test_prepare_runs_after_all_signals_before_exit_wait(self):
        self.run_kill([10, 11], lambda: self.events.append(('prepare',)))
        self.assertEqual(self.events, [('kill', 11, signal.SIGKILL),
                                      ('kill', 10, signal.SIGKILL),
                                      ('prepare',), ('wait', [10, 11])])

    def test_prepare_failure_still_waits_before_propagating(self):
        failure = OSError('cannot create restore workspace')
        def prepare():
            self.events.append(('prepare',))
            raise failure
        with self.assertRaises(OSError) as caught:
            self.run_kill([10], prepare)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.events[-1], ('wait', [10]))

    def test_empty_victim_set_still_prepares_and_waits(self):
        self.run_kill([], lambda: self.events.append(('prepare',)))
        self.assertEqual(self.events, [('prepare',), ('wait', [])])

    def test_other_kill_callers_keep_the_existing_barrier(self):
        self.run_kill([10])
        self.assertEqual(self.events, [('kill', 10, signal.SIGKILL), ('wait', [10])])


if __name__ == '__main__':
    unittest.main()
