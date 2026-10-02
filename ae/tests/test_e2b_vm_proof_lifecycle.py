"""Local-only VM observer lifecycle contracts; no services or real /proc sampling."""
import errno
import importlib.util
import json
import shutil
import stat
import tempfile
import unittest
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('vm_lifecycle_context', Path(__file__).resolve().parents[1]/'scripts/e2b_service_context.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class VMProofLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/'e2b'
        self.root.mkdir()
        self.child = self.root/'sbx-sandbox1-run1'
        self.child.mkdir()
        self.lstat = Path.lstat
        self.root_inode = self.lstat(self.root).st_ino
        self.child_inode = self.lstat(self.child).st_ino
        self.group = {'path': str(self.child), 'inode': self.child_inode,
                      'cpuset.cpus.effective': '72-75', 'cpuset.mems.effective': '3',
                      'memory.swap.max': 'max', 'memory.swap.current': '0'}
        self.root_group = dict(self.group, path=str(self.root), inode=self.root_inode, **{'memory.swap.max': '0'})
        self.task = {'pid': 42, 'start_ticks': 10, 'cgroup': '0::/e2b/'+self.child.name,
                     'cpus': '72-75', 'mems': '3', 'numa_policies': {'bind:3': 1}}
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(m, 'VM_ROOT', self.root))
        def trusted_stat(path):
            info = self.lstat(path)
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_mode=info.st_mode, st_uid=0)
        self.stack.enter_context(patch.object(Path, 'lstat', trusted_stat))
        self.cg = self.stack.enter_context(patch.object(m, 'cgroup', side_effect=lambda p: deepcopy(self.root_group if p == self.root else self.group)))
        self.procs = self.stack.enter_context(patch.object(m, 'cgroup_processes', return_value=[42]))
        self.started = self.stack.enter_context(patch.object(m, 'start_ticks', return_value=10))
        self.process = self.stack.enter_context(patch.object(m, 'process', side_effect=lambda pid, **kwargs: deepcopy(self.task)))
        self.threads = self.stack.enter_context(patch.object(m, 'threads', side_effect=lambda pid, **kwargs: [deepcopy(self.task)]))
        self.proof = m.VMProof(3, '72-75')

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def delete_and_error(self, code=errno.ENODEV):
        shutil.rmtree(self.child)
        raise OSError(code, 'synthetic lifecycle read')

    def test_deleted_owned_child_cgroup_enodev_is_discarded_not_proof(self):
        self.cg.side_effect = lambda p: deepcopy(self.root_group) if p == self.root else self.delete_and_error()
        self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        self.assertEqual(len(self.proof.discarded_cgroups), 1)
        discarded = self.proof.discarded_cgroups[0]
        self.assertEqual(discarded['inode'], self.child_inode)
        self.assertEqual(discarded['errno'], errno.ENODEV)
        self.assertEqual(discarded['operation'], 'cgroup-files')

    def test_deleted_owned_child_procs_enodev_is_discarded(self):
        self.procs.side_effect = lambda p: self.delete_and_error()
        self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        self.assertEqual(self.proof.discarded_cgroups[0]['operation'], 'cgroup.procs')

    def test_present_leaf_enodev_is_fatal(self):
        self.procs.side_effect = OSError(errno.ENODEV, 'present leaf')
        with self.assertRaisesRegex(RuntimeError, 'remains present'):
            self.proof.sample()
        self.assertEqual(self.proof.discarded_cgroups, [])

    def test_recreated_leaf_inode_is_fatal(self):
        def replaced(path):
            self.child.rename(self.root/'old-leaf')
            self.child.mkdir()
            raise OSError(errno.ENODEV, 'recreated')
        self.procs.side_effect = replaced
        with self.assertRaisesRegex(RuntimeError, 'replaced'):
            self.proof.sample()
        self.assertEqual(self.proof.rows, {})

    def test_root_missing_is_fatal_to_watch(self):
        self.cg.side_effect = FileNotFoundError(errno.ENOENT, 'root vanished')
        self.proof.observer = Mock(receipt={})
        self.proof.watch()
        self.assertEqual(len(self.proof.errors), 1)
        self.assertIn('FileNotFoundError', self.proof.errors[0])

    def test_root_enodev_is_fatal(self):
        self.cg.side_effect = OSError(errno.ENODEV, 'root unavailable')
        with self.assertRaises(OSError):
            self.proof.sample()
        self.assertEqual(self.proof.discarded_cgroups, [])

    def test_root_changed_or_unpinned_during_discard_is_fatal(self):
        def changed(path):
            shutil.rmtree(self.child)
            self.root_group['cpuset.mems.effective'] = '2'
            raise OSError(errno.ENODEV, 'deleted with invalid root')
        self.procs.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, 'placement differs'):
            self.proof.sample()
        self.assertEqual(self.proof.discarded_cgroups, [])

    def test_other_errno_in_deleted_leaf_is_fatal(self):
        for code in (errno.EACCES, errno.EIO):
            with self.subTest(code=code):
                self.procs.side_effect = OSError(code, 'not an admitted disappearance')
                with self.assertRaises(OSError):
                    self.proof.sample()
        self.assertEqual(self.proof.discarded_cgroups, [])

    def test_proc_enodev_is_fatal_even_if_pid_disappears(self):
        self.process.side_effect = OSError(errno.ENODEV, 'proc read not exempt')
        with self.assertRaises(OSError):
            self.proof.sample()
        self.assertEqual(self.proof.discarded_cgroups, [])

    def test_pid_reuse_fails(self):
        self.started.side_effect = [10, 11]
        with self.assertRaisesRegex(RuntimeError, 'PID was reused'):
            self.proof.sample()
        self.assertEqual(self.proof.rows, {})

    def test_disappearing_thread_does_not_leave_partial_success_row(self):
        self.threads.side_effect = FileNotFoundError(errno.ENOENT, 'thread exited')
        self.proof.sample()
        self.assertEqual(self.proof.rows, {})

    def test_actual_placement_swap_and_policy_errors_still_fail(self):
        changes = [('cpus', '0-3'), ('numa_policies', {'bind:2': 1})]
        for key, value in changes:
            original = self.task[key]
            self.task[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.proof.sample()
            self.task[key] = original
        self.group['memory.swap.current'] = '4096'
        with self.assertRaisesRegex(RuntimeError, 'existing swap'):
            self.proof.sample()
        self.assertEqual(self.proof.rows, {})

    def test_observed_bad_cgroup_is_fatal_before_later_disappearance(self):
        for key, bad in [('cpuset.mems.effective', '2'), ('memory.swap.current', '4096')]:
            original = self.group[key]
            self.group[key] = bad
            self.procs.side_effect = lambda p: self.delete_and_error()
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.proof.sample()
            self.procs.assert_not_called()
            self.group[key] = original
        self.assertEqual(self.proof.discarded_cgroups, [])

    def test_observed_bad_task_policy_is_fatal_before_thread_disappears(self):
        self.task['numa_policies'] = {'bind:2': 1}
        self.threads.side_effect = FileNotFoundError(errno.ENOENT, 'thread exited')
        with self.assertRaisesRegex(RuntimeError, 'memory policy differs'):
            self.proof.sample()
        self.threads.assert_not_called()
        self.assertEqual(self.proof.rows, {})

    def test_replaced_leaf_during_process_reads_is_fatal(self):
        def replaced(pid, **kwargs):
            self.child.rename(self.root/'old-leaf')
            self.child.mkdir()
            return [deepcopy(self.task)]
        self.threads.side_effect = replaced
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            self.proof.sample()
        self.assertEqual(self.proof.rows, {})

    def test_deleted_leaf_during_process_reads_discards_complete_read(self):
        def deleted(pid, **kwargs):
            shutil.rmtree(self.child)
            return [deepcopy(self.task)]
        self.threads.side_effect = deleted
        self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        self.assertEqual(self.proof.discarded_cgroups[0]['operation'], 'completed-sample-recheck')

    def test_replaced_root_during_discard_is_fatal(self):
        def replaced(path):
            shutil.rmtree(self.child)
            self.root.rename(self.root.parent/'old-root')
            self.root.mkdir()
            raise OSError(errno.ENODEV, 'root replaced')
        self.procs.side_effect = replaced
        with self.assertRaisesRegex(RuntimeError, 'root identity changed'):
            self.proof.sample()
        self.assertEqual(self.proof.discarded_cgroups, [])

    def test_discarded_child_does_not_satisfy_full_id_coverage(self):
        self.procs.side_effect = lambda p: self.delete_and_error()
        self.proof.sample()
        output = self.root/'fanout.json'
        output.write_text(json.dumps([{'children': [{'sandbox_id': 'sandbox1'}]}]))
        with self.assertRaisesRegex(RuntimeError, 'Missing placement proof'):
            self.proof.verify_ids(output)

    def test_complete_valid_sample_retains_threads_and_identity(self):
        self.proof.sample()
        self.assertEqual(len(self.proof.rows), 1)
        row = next(iter(self.proof.rows.values()))
        self.assertEqual(row['tasks'], [self.task])
        self.assertEqual(row['threads'], [self.task])
        self.assertIn(self.child.name+':42:10', self.proof.rows)
        self.assertEqual(self.proof.discarded_cgroups, [])


if __name__ == '__main__':
    unittest.main()
