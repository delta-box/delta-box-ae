"""Cold restore must release old PID slots, not merely observe process exit."""
from contextlib import redirect_stdout
import io
import os
import signal
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backends.deltabox.gsd import sandbox_controller as sc


class ExternalRestoreReapingTests(unittest.TestCase):
    def controller(self):
        c = sc.SandboxController.__new__(sc.SandboxController)
        c.ns_init_pid = 900
        c.agent_pid = 100
        c.fixed_active_pid = 100
        c._pending_cleanup_reaps = {200: {(10, 100)}}
        c._last_killed_active_pids = [100, 101]
        c.template_pool = SimpleNamespace(
            templates={'a': 200, 'b': 300}, request_reap_children=Mock())
        c._check_external_restore_pool_clear_allowed = Mock()
        c._pid_gone_or_zombie = Mock(side_effect=lambda pid: pid == 300)
        c._collect_subtree_pids = Mock(side_effect=lambda pid: [pid, pid + 1])
        return c

    def test_fixed_launcher_reaper_uses_no_template_fifo_and_waits_all_old_tasks(self):
        c = self.controller()
        events = []
        c._kill_pids_and_wait = Mock(side_effect=lambda *args: events.append('kill'))

        def barrier(pids):
            self.assertEqual(pids, {100, 101, 200, 201, 300})
            self.assertEqual(c.template_pool.templates, {})
            self.assertEqual(c._pending_cleanup_reaps, {})
            events.append('barrier')

        c._wait_external_restore_reaped = Mock(side_effect=barrier)
        c._clear_templates_for_external_restore()
        self.assertEqual(events, ['kill', 'barrier'])
        c.template_pool.request_reap_children.assert_not_called()

    def test_agent_init_reaper_keeps_its_protocol_before_barrier(self):
        c = self.controller()
        c.fixed_active_pid = None
        events = []
        c._kill_pids_and_wait = Mock(side_effect=lambda *args: events.append('kill'))
        c.template_pool.request_reap_children.side_effect = lambda *a, **k: events.append('reap')
        c._wait_external_restore_reaped = Mock(side_effect=lambda pids: events.append('barrier'))
        with patch.object(sc.os.path, 'isdir', return_value=True):
            c._clear_templates_for_external_restore()
        self.assertEqual(events, ['kill', 'reap', 'barrier'])
        c.template_pool.request_reap_children.assert_called_once_with(900, timeout=0.2)

    def test_no_template_pool_still_waits_for_active_subtree_reaping(self):
        c = self.controller()
        c.template_pool = None
        c._wait_external_restore_reaped = Mock()
        c._clear_templates_for_external_restore()
        c._wait_external_restore_reaped.assert_called_once_with({100, 101})

    def test_unreadable_proc_is_not_a_free_slot_and_timeout_diagnoses_it(self):
        c = self.controller()
        output = io.StringIO()
        with patch('builtins.open', side_effect=PermissionError('proc denied')), \
                patch.object(sc.os, 'readlink', side_effect=PermissionError('ns denied')), \
                patch.object(sc.os, 'kill') as kill, \
                patch.object(sc, '_trace_event') as trace, redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'unreaped host PIDs.*100'):
                c._wait_external_restore_reaped([100], timeout=0)
        kill.assert_not_called()
        self.assertIn('proc denied', output.getvalue())
        self.assertIn('ns denied', output.getvalue())
        trace.assert_called_once()
        self.assertEqual(trace.call_args.kwargs['pids'], [100])

    def test_missing_stat_proves_slot_released_without_namespace_query(self):
        c = self.controller()
        with patch('builtins.open', side_effect=FileNotFoundError), \
                patch.object(sc.os, 'readlink', side_effect=AssertionError('namespace probe')):
            c._wait_external_restore_reaped([100], timeout=0)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux zombie process required')
    def test_real_zombie_blocks_until_parent_waitpid_releases_pid(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        # WNOWAIT observes a stable zombie but deliberately leaves its PID held.
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        c = self.controller()
        c.ns_init_pid = os.getpid()
        reaped = threading.Event()

        def reap_later():
            time.sleep(0.03)
            os.waitpid(pid, 0)
            reaped.set()

        worker = threading.Thread(target=reap_later)
        worker.start()
        try:
            with patch.object(sc.os, 'readlink', side_effect=OSError('no ns for zombie')):
                c._wait_external_restore_reaped([pid], timeout=1)
            self.assertFalse(os.path.exists(f'/proc/{pid}/stat'))
        finally:
            worker.join(2)
        self.assertTrue(reaped.is_set())

    @unittest.skipUnless(sys.platform == 'linux', 'Linux zombie process required')
    def test_real_unreaped_zombie_times_out_with_status_and_nspid(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        c = self.controller()
        c.ns_init_pid = os.getpid()
        output = io.StringIO()
        try:
            with patch.object(sc.os, 'kill') as kill, \
                    patch.object(sc, '_trace_event'), redirect_stdout(output):
                with self.assertRaisesRegex(RuntimeError, 'unreaped host PIDs'):
                    c._wait_external_restore_reaped([pid], timeout=0.005)
            kill.assert_not_called()
            self.assertIn('Z (zombie)', output.getvalue())
            self.assertIn('NSpid:', output.getvalue())
            self.assertIn(str(pid), output.getvalue())
        finally:
            os.waitpid(pid, 0)


if __name__ == '__main__':
    unittest.main()
