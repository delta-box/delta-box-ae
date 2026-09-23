"""Reject unavailable cold restores before changing the active sandbox."""
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backends.deltabox.gsd import sandbox_controller as sc


class ColdRestorePreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(sc.os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.trace = patch.object(sc, '_trace_event')
        self.trace.start()
        self.addCleanup(self.trace.stop)
        c = self.controller = sc.SandboxController.__new__(sc.SandboxController)
        c.registry = {'target': {'layers': ['/saved'], 'parent_id': None,
                                 'external_pidns': False}}
        c.root_overlays = Mock()
        c.root_overlays.has_checkpoint.return_value = True
        c.template_pool = SimpleNamespace(
            get=Mock(return_value=None), last_fork_meta={},
            dispatch_fork=Mock(return_value=True),
            await_fork=Mock(return_value=(777, 202)))
        c._join_active_dump_before_kill = Mock(return_value=0.0)
        c._kill_active_subtree = Mock(side_effect=lambda *, during_exit: during_exit())
        c._apply_overlay_switch = Mock()
        c._schedule_async_parent_restamp = Mock()
        c._bump_epoch = Mock()
        c._clear_soft_dirty_after_fork = Mock()
        c.layers_root = self.tmp.name
        c.fixed_active_pid = None
        c.restore_fastfork_dump_pid = False
        c.enable_prewarm = False
        c.enable_criu_lazy_restore = False
        c.parallel_lazy_restore = False

    def assert_active_untouched(self):
        c = self.controller
        c.root_overlays.restore.assert_not_called()
        c._join_active_dump_before_kill.assert_not_called()
        c._kill_active_subtree.assert_not_called()
        c._apply_overlay_switch.assert_not_called()
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_failed_dump_does_not_change_filesystem_or_kill_active(self):
        future = Future()
        future.set_exception(RuntimeError('dump failed'))
        self.controller.registry['target']['dump_future'] = future
        with self.assertRaises(sc.DumpUnavailableError):
            self.controller.restore_action('target')
        self.assert_active_untouched()

    def test_dump_timeout_does_not_change_filesystem_or_kill_active(self):
        future = Mock()
        future.result.side_effect = FutureTimeout('unfinished dump')
        self.controller.registry['target']['dump_future'] = future
        with self.assertRaises(sc.DumpUnavailableError):
            self.controller.restore_action('target')
        future.result.assert_called_once_with(timeout=30.0)
        self.assert_active_untouched()

    def test_recorded_dump_errors_reject_before_mutation(self):
        for error in ({'dump_error': 'failed'}, {'dump_stats': {'dump_error': 'failed'}}):
            with self.subTest(error=error):
                self.controller.registry['target'] = {'layers': ['/saved'], **error}
                with self.assertRaises(sc.DumpUnavailableError):
                    self.controller.restore_action('target')
                self.assert_active_untouched()

    def test_external_pool_clear_denial_precedes_mutation(self):
        future = Future()
        future.set_result(0)
        self.controller.registry['target'].update(external_pidns=True, dump_future=future)
        with self.assertRaisesRegex(sc.DumpUnavailableError, 'clear warm-template'):
            self.controller.restore_action('target')
        self.assert_active_untouched()

    def test_effective_parent_is_checked_before_root_rollback(self):
        self.controller.registry['light'] = {'effective_restore_id': 'target'}
        self.controller.registry['target']['dump_error'] = 'parent dump failed'
        with self.assertRaises(sc.DumpUnavailableError):
            self.controller.restore_action('light')
        self.assert_active_untouched()

    def test_forced_cold_restore_checks_dump_before_mutation(self):
        self.controller.template_pool.get.return_value = 777
        self.controller.registry['target']['dump_error'] = 'failed'
        with patch.dict(sc.os.environ, {'DELTABOX_FORCE_CRIU_RESTORE': '1'}):
            with self.assertRaises(sc.DumpUnavailableError):
                self.controller.restore_action('target')
        self.assert_active_untouched()

    def test_active_dump_join_failure_precedes_root_rollback_and_kill(self):
        c = self.controller
        c._join_active_dump_before_kill.side_effect = RuntimeError('active dump join failed')
        with self.assertRaisesRegex(RuntimeError, 'active dump join failed'):
            c.restore_action('target')
        c._join_active_dump_before_kill.assert_called_once_with()
        c.root_overlays.restore.assert_not_called()
        c._kill_active_subtree.assert_not_called()
        c._apply_overlay_switch.assert_not_called()
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_warm_success_never_waits_for_target_dump_or_checks_cold_permission(self):
        c = self.controller
        future = Mock()
        future.result.side_effect = AssertionError('warm restore joined target dump')
        future.done.side_effect = AssertionError('warm restore polled target dump')
        c.registry['target'].update(external_pidns=True, dump_future=future,
                                    dump_error='durable copy unavailable')
        c.template_pool.get.return_value = 777
        with patch.object(sc, '_read_rss_mb', return_value=20.0), \
             patch.object(c, '_is_external_pidns_dump', side_effect=AssertionError('cold check')):
            result = c.restore_action('target')
        self.assertEqual(result['path'], 'warm-template')
        self.assertEqual(result['restore_dump_join_ms'], 0.0)
        future.result.assert_not_called()
        future.done.assert_not_called()
        c.root_overlays.restore.assert_called_once_with('target')
        c._kill_active_subtree.assert_called_once()

    def test_clear_operation_still_requires_explicit_permission(self):
        c = self.controller
        c.template_pool = None
        c.ns_init_pid, c.agent_pid = 10, 101
        c._pending_cleanup_reaps = {}
        c._wait_external_restore_reaped = Mock()
        with self.assertRaisesRegex(sc.DumpUnavailableError, 'clear warm-template'):
            c._clear_templates_for_external_restore()
        for flag in ('DELTABOX_FORCE_CRIU_RESTORE', 'DELTABOX_ALLOW_COLD_RESTORE_POOL_CLEAR'):
            with self.subTest(flag=flag), patch.dict(sc.os.environ, {flag: '1'}):
                c._clear_templates_for_external_restore()
        self.assert_active_untouched()


if __name__ == '__main__':
    unittest.main()
