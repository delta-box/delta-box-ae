"""Live-only Figure 6 checkpoints share core restore without durable fallback."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backends.deltabox.gsd import sandbox_controller as sc

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "_test_paper_memory_policy", ROOT / "replay/guest/paper_memory_policy.py")
policy = importlib.util.module_from_spec(_spec)
# The guest installs core modules flat; tests use the same actual core via its
# package name without loading a second controller implementation.
with patch.dict(sys.modules, {"sandbox_controller": sc}):
    _spec.loader.exec_module(policy)


class ColdPathReached(Exception):
    pass


class PaperMemoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = []
        c = self.controller = policy.ForkOnlyController.__new__(policy.ForkOnlyController)
        c.agent_pid, c.ns_init_pid = 101, 10
        c.snapshot_store = str(Path(self.tmp.name) / 'snapshots')
        c.layers_root = str(Path(self.tmp.name) / 'layers')
        c.current_upper = str(Path(self.tmp.name) / 'upper')
        c.current_work = str(Path(self.tmp.name) / 'work')
        Path(c.current_upper).mkdir()
        c.base_layer = '/base'
        c.fixed_active_pid = 100
        c.restore_fastfork_dump_pid = False
        c.enable_prewarm = False
        c.enable_warm_template = True
        c.enable_adaptive = False
        c.enable_incremental_dump = False
        c.enable_criu_lazy_restore = False
        c.parallel_lazy_restore = False
        c.async_template_full_dump = False
        c.prefork_template_dump = False
        c.checkpoint_stash_template = True
        c.root_overlays = None
        c.registry = {'target': {'layers': ['/saved', '/base'], 'parent_id': None,
                                 'mem_path': str(Path(c.snapshot_store) / 'target')}}
        c.template_pool = SimpleNamespace(
            templates={'target': 777}, last_fork_meta={},
            get=lambda rid: c.template_pool.templates.get(rid),
            dispatch_fork=Mock(side_effect=lambda *a, **k: self.event('dispatch', True)),
            await_fork=Mock(side_effect=lambda *a, **k: self.event('await-fork', (777, 202))),
            request_stash_template=Mock(side_effect=self.stash),
        )
        c._join_active_dump_before_kill = Mock(return_value=0.0)
        c._kill_active_subtree = self.kill_active
        c._apply_overlay_switch = Mock(side_effect=lambda *a, **k: self.event('switch'))
        c._schedule_async_parent_restamp = Mock(side_effect=lambda *a: self.event('restamp'))
        c._bump_epoch = Mock(side_effect=lambda: self.event('epoch'))
        c._clear_soft_dirty_after_fork = Mock(side_effect=lambda *a: self.event('clear-dirty'))
        c._drain_pending_template_cleanup = Mock()
        c._bootstrap_active_before_dump = Mock(return_value=(None, 0.0, False))
        c._current_dump_tree_pid = Mock(return_value=101)
        c._is_pidns_init = Mock(return_value=False)
        # Hitting this marker proves the default method still reaches durable
        # restore; no subprocess, actual PID or kernel ioctl is used in tests.
        c._is_external_pidns_dump = Mock(side_effect=ColdPathReached)
        self.addCleanup(patch.stopall)
        patch.dict(sc.os.environ, {}, clear=True).start()
        patch.object(sc, '_read_rss_mb', side_effect=lambda *a: self.event('rss', 20.0)).start()
        patch.object(policy, '_read_rss_mb', return_value=20.0).start()
        patch.object(sc.subprocess, 'check_call', side_effect=AssertionError('CRIU forbidden')).start()
        patch.object(sc.subprocess, 'Popen', side_effect=AssertionError('CRIU forbidden')).start()

    def event(self, name, result=None):
        self.events.append(name)
        return result

    def kill_active(self, *, during_exit=None):
        self.event('kill')
        if during_exit:
            during_exit()
        self.event('exit-wait-complete')

    def stash(self, active, snapshot, **kwargs):
        self.controller.template_pool.templates[snapshot] = 777
        return 777

    def test_checkpoint_and_restore_never_call_criu_and_share_exit_barrier(self):
        c = self.controller
        # Avoid signalling a real PID when checkpoint stops existing templates.
        c.template_pool.templates.clear()
        checkpoint = c.checkpoint_action('', 'checkpoint', raw_command='standard')
        self.assertTrue(checkpoint['fork_only'])
        self.assertIsNone(checkpoint['dump_future'])
        self.assertEqual(checkpoint['dump_size_bytes'], 0)
        self.assertTrue((Path(checkpoint['mem_path']) / 'fork_only_marker.txt').is_file())
        c.enable_prewarm = True
        with patch.object(sc, 'spawn_prewarm', side_effect=lambda *a: self.event('prewarm')):
            restored = c.restore_action(checkpoint['id'])
        self.assertEqual(c.agent_pid, 202)
        self.assertEqual(restored['path'], 'warm-template')
        self.assertIn('restore_prepare_overlapped_ms', restored)
        self.assertLess(self.events.index('rss'), self.events.index('exit-wait-complete'))
        self.assertLess(self.events.index('exit-wait-complete'), self.events.index('dispatch'))
        self.assertLess(self.events.index('exit-wait-complete'), self.events.index('switch'))
        self.assertLess(self.events.index('clear-dirty'), self.events.index('prewarm'))
        c._is_external_pidns_dump.assert_not_called()

    def test_missing_template_fails_before_root_rollback_or_active_kill(self):
        c = self.controller
        c.template_pool.templates.clear()
        c.root_overlays = Mock()
        with self.assertRaisesRegex(RuntimeError, 'requires its live template'):
            c.restore_action('target')
        c.root_overlays.restore.assert_not_called()
        c._join_active_dump_before_kill.assert_not_called()
        self.assertEqual(self.events, [])

    def test_force_criu_cannot_override_live_only_policy(self):
        c = self.controller
        c.root_overlays = Mock()
        with patch.dict(sc.os.environ, {'DELTABOX_FORCE_CRIU_RESTORE': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'durable fallback is prohibited'):
                c.restore_action('target')
        c.root_overlays.restore.assert_not_called()
        self.assertEqual(self.events, [])

    def test_failed_fork_cannot_fall_back_or_join_a_durable_dump(self):
        c = self.controller
        c.template_pool.await_fork.side_effect = None
        c.template_pool.await_fork.return_value = (777, None)
        future = c.registry['target']['dump_future'] = Mock()
        with self.assertRaisesRegex(RuntimeError, 'template fork failed'):
            c.restore_action('target')
        future.result.assert_not_called()
        c._is_external_pidns_dump.assert_not_called()
        self.assertIn('exit-wait-complete', self.events)
        self.assertNotIn('epoch', self.events)

    def test_default_core_still_falls_back_on_missing_or_failed_template(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                c = self.controller
                c.template_pool.templates = {} if missing else {'target': 777}
                c.template_pool.await_fork.side_effect = None
                c.template_pool.await_fork.return_value = (777, None)
                with self.assertRaises(ColdPathReached):
                    sc.SandboxController.restore_action(c, 'target')

    def test_lightweight_restore_uses_effective_parent_and_preserves_replay(self):
        c = self.controller
        commands = [{'action': 'ViewCode', 'worker_ops': [{'op': 'read'}]}]
        c.registry['light'] = {'effective_restore_id': 'target', 'replay_cmds': commands}
        restored = c.restore_action('light')
        self.assertEqual(restored['replay_cmds'], commands)
        c._apply_overlay_switch.assert_called_once()
        self.assertEqual(c._apply_overlay_switch.call_args.args[0], ['/saved', '/base'])

    def test_checkpoint_drift_guard_reports_exact_hashes(self):
        with patch.object(policy, '_EXPECTED_CHECKPOINT_ACTION', 'not-the-current-hash'):
            with self.assertRaisesRegex(RuntimeError, 'checkpoint_action; expected=.*actual='):
                policy.verify_runtime_compatibility()

    def test_old_core_without_explicit_fallback_policy_is_rejected(self):
        with patch.object(sc.SandboxController, 'restore_action', lambda self, target: None):
            with self.assertRaisesRegex(RuntimeError, 'explicit allow_cold_fallback policy'):
                policy.verify_runtime_compatibility()


if __name__ == '__main__':
    unittest.main()
