"""An unfinished dump remains protected before its stash helper reparents it."""
from concurrent.futures import Future
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from backends.deltabox.gsd.sandbox_controller import SandboxController


class AsyncDumpLifetimeTest(unittest.TestCase):
    def controller(self, registry):
        c = SandboxController.__new__(SandboxController)
        c.agent_pid = 10
        c.ns_init_pid = 1
        c.fixed_active_pid = None
        c.restore_fastfork_dump_pid = False
        c.template_pool = SimpleNamespace(templates={'warm': 20})
        c.registry = registry
        c._join_active_dump_before_kill = Mock(return_value=0.0)
        c._pid_ppid = Mock(return_value=1)
        c._pid_as_seen_by = Mock(return_value=10)
        c._wait_fixed_active_slot_free = Mock(return_value=True)
        c._queue_template_cleanup = Mock()
        c._kill_pids_and_wait = Mock()
        return c

    def test_restore_excludes_pending_dump_subtree_without_waiting(self):
        pending = Future()
        c = self.controller({'checkpoint': {'async_dump_template_pid': 30, 'dump_future': pending}})
        # active -> helper -> {warm, dump_root}; dump_root may have descendants.
        tree = {10: [11], 11: [20, 30], 30: [31]}
        visited = []
        def walk(pid, excluded):
            if pid in excluded:
                return []
            visited.append(pid)
            return [pid] + [p for child in tree.get(pid, []) for p in walk(child, excluded)]
        c._collect_subtree_pids = walk
        c._kill_active_subtree()
        c._kill_pids_and_wait.assert_called_once_with([10, 11], '_kill_active_subtree')
        self.assertEqual(visited, [10, 11])
        self.assertFalse(pending.done())

    def test_all_inflight_dumps_protected_but_completed_clones_disposable(self):
        completed = Future(); completed.set_result(None)
        c = self.controller({
            'old': {'async_dump_template_pid': 30, 'dump_future': Future()},
            'new': {'async_dump_template_pid': 40, 'dump_future': Future()},
            'done': {'async_dump_template_pid': 50, 'dump_future': completed},
            'lightweight': {},
        })
        c._collect_subtree_pids = Mock(return_value=[10, 11, 50])
        c._kill_active_subtree()
        c._collect_subtree_pids.assert_called_once_with(10, {20, 30, 40})
        c._kill_pids_and_wait.assert_called_once_with([10, 11, 50], '_kill_active_subtree')

    def test_non_async_path_keeps_previous_exclusions(self):
        c = self.controller({'incremental': {'dump_tree_pid': 10, 'dump_future': Future()}})
        c._collect_subtree_pids = Mock(return_value=[10])
        c._kill_active_subtree()
        c._join_active_dump_before_kill.assert_called_once()
        c._collect_subtree_pids.assert_called_once_with(10, {20})

    def test_prepare_callback_keeps_pending_dump_protected(self):
        c = self.controller({'checkpoint': {'async_dump_template_pid': 30, 'dump_future': Future()}})
        c._collect_subtree_pids = Mock(return_value=[10, 11])
        prepare = Mock()
        c._kill_active_subtree(during_exit=prepare)
        c._collect_subtree_pids.assert_called_once_with(10, {20, 30})
        args = c._kill_pids_and_wait.call_args.args
        self.assertEqual(args[:2], ([10, 11], '_kill_active_subtree'))
        args[2]()
        prepare.assert_called_once_with()

    def test_failed_prepare_finishes_fixed_pid_reap(self):
        c = self.controller({})
        c.fixed_active_pid = 10
        c._pid_ppid.return_value = 20
        c._collect_subtree_pids = Mock(return_value=[10])
        c._translate_pid_in_ns = Mock(return_value=10)
        c.template_pool.request_reap_pid = Mock(return_value={'ok': True})
        failure = OSError('ENOSPC')
        c._kill_pids_and_wait.side_effect = lambda pids, label, prepare: prepare()
        with self.assertRaises(OSError) as caught:
            c._kill_active_subtree(during_exit=Mock(side_effect=failure))
        self.assertIs(caught.exception, failure)
        c.template_pool.request_reap_pid.assert_called_once_with(20, 10, timeout=1.0)
        c._wait_fixed_active_slot_free.assert_called_once()
        c._queue_template_cleanup.assert_not_called()

    def test_failed_prepare_still_queues_deferred_cleanup(self):
        c = self.controller({})
        c._pid_ppid.return_value = 20
        c._collect_subtree_pids = Mock(return_value=[10])
        failure = OSError('ENOSPC')
        c._kill_pids_and_wait.side_effect = lambda pids, label, prepare: prepare()
        with self.assertRaises(OSError) as caught:
            c._kill_active_subtree(during_exit=Mock(side_effect=failure))
        self.assertIs(caught.exception, failure)
        c._wait_fixed_active_slot_free.assert_called_once()
        c._queue_template_cleanup.assert_called_once_with(20, 10, 10)


if __name__ == '__main__':
    unittest.main()
