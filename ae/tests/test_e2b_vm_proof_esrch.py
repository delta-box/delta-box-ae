"""Bound VM /proc ESRCH contracts over temporary proc/cgroup files only."""
import errno
import importlib.util
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('vm_esrch_context', Path(__file__).resolve().parents[1]/'scripts/e2b_service_context.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class VMProofESRCH(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.proc = self.base/'proc'
        self.proc.mkdir()
        self.root = self.base/'e2b'
        self.child = self.root/'sbx-sandbox1-run1'
        self.child.mkdir(parents=True)
        for path in (self.root, self.child):
            for name in m.CG_FILES:
                value = {'cpuset.cpus.effective':'72-75', 'cpuset.mems.effective':'3',
                         'memory.swap.current':'0', 'memory.swap.max':'0' if path == self.root else 'max'}.get(name, '')
                (path/name).write_text(value+'\n')
            (path/'cgroup.procs').write_text('42\n' if path == self.child else '')
        self.make_proc(42, 42)
        (self.proc/'42/task').mkdir()
        (self.proc/'42/task/42').symlink_to(self.proc/'42', target_is_directory=True)
        self.make_proc(43, 42)
        (self.proc/'42/task/43').symlink_to(self.proc/'43', target_is_directory=True)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(m, 'PROC', self.proc))
        self.stack.enter_context(patch.object(m, 'VM_ROOT', self.root))
        self.stack.enter_context(patch.object(m, 'start_ticks', side_effect=lambda pid: m.proc_start(self.proc/str(pid))))
        original_stat = Path.lstat
        def trusted(path):
            row = original_stat(path)
            return SimpleNamespace(st_dev=row.st_dev, st_ino=row.st_ino, st_mode=row.st_mode, st_uid=0)
        self.stack.enter_context(patch.object(Path, 'lstat', trusted))
        self.proof = m.VMProof(3, '72-75')
        self.read_text = Path.read_text
        self.failing_reads = 0

    def make_proc(self, pid, tgid, start=10):
        p = self.proc/str(pid)
        p.mkdir()
        self.set_start(pid, start)
        (p/'status').write_text(f'Tgid:\t{tgid}\nCpus_allowed_list:\t72-75\nMems_allowed_list:\t3\n')
        (p/'cgroup').write_text('0::/e2b/'+self.child.name+'\n')
        (p/'numa_maps').write_text('123 bind:3 N3=1\n')
        (p/'exe').symlink_to('/usr/bin/firecracker')

    def set_start(self, pid, start):
        (self.proc/str(pid)/'stat').write_text(str(pid)+' (test) '+' '.join(['S']+['0']*18+[str(start)]))

    def remove_target(self, pid):
        task = self.proc/'42/task'/str(pid)
        if task.is_symlink():
            task.unlink()
        shutil.rmtree(self.proc/str(pid))

    def fail_read(self, pid=43, action=None, code=errno.ESRCH, filename='numa_maps'):
        target = self.proc/str(pid)/filename
        def read(path, *args, **kwargs):
            if path == target:
                self.failing_reads += 1
                if action is not None:
                    action()
                raise OSError(code, 'synthetic exact read', str(path))
            return self.read_text(path, *args, **kwargs)
        self.stack.enter_context(patch.object(Path, 'read_text', read))

    def assert_fatal(self, error=RuntimeError):
        with self.assertRaises(error):
            self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        self.assertEqual(self.proof.discarded_processes, [])

    def test_valid_real_file_sample_keeps_all_threads(self):
        self.proof.sample()
        row = next(iter(self.proof.rows.values()))
        self.assertEqual([p['pid'] for p in row['tasks']], [42])
        self.assertEqual({p['pid'] for p in row['threads']}, {42, 43})
        self.assertEqual(self.proof.discarded_processes, [])

    def test_thread_esrch_and_independent_absence_discards_entire_sample(self):
        self.fail_read(action=lambda: self.remove_target(43))
        self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        row = self.proof.discarded_processes[0]
        self.assertEqual((row['target']['pid'], row['target']['start_ticks'], row['target']['tgid']), (43, 10, 42))
        self.assertEqual(row['owner_after'], 'same owner; target thread absent')
        self.assertEqual(row['path'], str(self.proc/'43/numa_maps'))
        self.assertEqual(row['errno'], errno.ESRCH)
        self.assertGreaterEqual(row['verified_at'], row['error_observed_at'])
        self.assertEqual(row['child']['inode'], self.child.lstat().st_ino)
        fanout = self.base/'fanout.json'
        fanout.write_text(json.dumps([{'children':[{'sandbox_id':'sandbox1'}]}]))
        with self.assertRaisesRegex(RuntimeError, 'Missing placement proof'):
            self.proof.verify_ids(fanout)

    def test_leader_esrch_and_absence_is_recorded(self):
        self.fail_read(42, action=lambda: (self.remove_target(43), self.remove_target(42)))
        self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        self.assertEqual(self.proof.discarded_processes[0]['owner_after'], 'absent')

    def test_thread_and_owner_absent_are_bound_not_reused(self):
        self.fail_read(action=lambda: (self.remove_target(43), self.remove_target(42)))
        self.proof.sample()
        self.assertEqual(self.proof.discarded_processes[0]['owner_after'], 'absent')

    def test_live_or_zombie_target_esrch_is_fatal(self):
        original = (self.proc/'43/stat').read_text()
        self.fail_read()
        for state in ('S', 'Z'):
            with self.subTest(state=state):
                (self.proc/'43/stat').write_text(original.replace(') S ', ') '+state+' '))
                self.assert_fatal()

    def test_reused_target_is_fatal(self):
        self.fail_read(action=lambda: self.set_start(43, 11))
        self.assert_fatal()

    def test_reused_owner_is_fatal(self):
        self.fail_read(action=lambda: (self.remove_target(43), self.set_start(42, 11)))
        self.assert_fatal()

    def test_stale_owner_task_membership_is_fatal(self):
        self.fail_read(action=lambda: shutil.rmtree(self.proc/'43'))
        self.assert_fatal()

    def test_target_not_in_bound_owner_task_directory_is_fatal_before_numa_maps(self):
        (self.proc/'42/task/43').unlink()
        # The leader is still enumerated, but the fake thread is directly observed
        # through the same validator to exercise membership, not an exception mock.
        group, _, identity = self.proof._child_inputs(self.child, (self.root.lstat().st_dev, self.root.lstat().st_ino))
        validate = lambda row, **kw: self.proof._validate_process(row, 42, 10, self.child, identity,
            (self.root.lstat().st_dev, self.root.lstat().st_ino), group, **kw)
        self.fail_read()
        with self.assertRaises(FileNotFoundError):
            m.process(43, before_numa_maps=validate)
        self.assertEqual(self.failing_reads, 0)

    def test_wrong_status_mask_cannot_be_hidden_by_later_cgroup_disappearance(self):
        p = self.proc/'42/status'
        p.write_text(p.read_text().replace('72-75', '0-3'))
        self.fail_read(42, code=errno.ENOENT, filename='cgroup')
        self.assert_fatal()
        self.assertEqual(self.failing_reads, 0)

    def test_known_leader_reuse_cannot_be_hidden_by_later_cgroup_disappearance(self):
        self.fail_read(42, code=errno.ENOENT, filename='cgroup')
        with patch.object(m, 'start_ticks', return_value=9):
            self.assert_fatal()
        self.assertEqual(self.failing_reads, 0)

    def test_wrong_tgid_cannot_be_hidden_by_later_esrch(self):
        p = self.proc/'43/status'
        p.write_text(p.read_text().replace('Tgid:\t42', 'Tgid:\t99'))
        self.fail_read(action=lambda: self.remove_target(43))
        self.assert_fatal()
        self.assertEqual(self.failing_reads, 0)

    def test_cgroup_prefix_collision_is_fatal_before_esrch(self):
        (self.proc/'43/cgroup').write_text('0::/e2b/'+self.child.name+'FOREIGN\n')
        self.fail_read(action=lambda: self.remove_target(43))
        self.assert_fatal()
        self.assertEqual(self.failing_reads, 0)

    def test_known_policy_violation_precedes_later_thread_esrch(self):
        (self.proc/'42/numa_maps').write_text('123 bind:2 N2=1\n')
        self.fail_read(action=lambda: self.remove_target(43))
        self.assert_fatal()
        self.assertEqual(self.failing_reads, 0)

    def test_child_replacement_during_esrch_is_fatal(self):
        def replace():
            self.remove_target(43)
            self.child.rename(self.root/'previous')
            self.child.mkdir()
        self.fail_read(action=replace)
        self.assert_fatal()

    def test_root_replacement_during_esrch_is_fatal(self):
        def replace():
            self.remove_target(43)
            self.root.rename(self.base/'previous')
            self.root.mkdir()
        self.fail_read(action=replace)
        self.assert_fatal()

    def test_root_disappearance_during_esrch_is_fatal_not_swallowed(self):
        self.fail_read(action=lambda: (self.remove_target(43), shutil.rmtree(self.root)))
        self.assert_fatal(FileNotFoundError)

    def test_post_exit_swap_violation_is_fatal(self):
        self.fail_read(action=lambda: (self.remove_target(43), (self.child/'memory.swap.current').write_text('4096')))
        self.assert_fatal()

    def test_non_esrch_numa_maps_error_is_fatal(self):
        self.fail_read(action=lambda: self.remove_target(43), code=errno.EACCES)
        self.assert_fatal(PermissionError)

    def test_cgroup_esrch_and_independent_absence_discards_sample(self):
        self.fail_read(filename='cgroup', action=lambda: self.remove_target(43))
        self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        row = self.proof.discarded_processes[0]
        self.assertEqual(row['operation'], 'cgroup')
        self.assertEqual(row['target']['pid'], 43)
        self.assertEqual(row['owner_start_ticks'], 10)


    def test_default_process_does_not_admit_esrch(self):
        def read(path, *args, **kwargs):
            relative = path.relative_to('/proc')
            if relative.name == 'numa_maps':
                raise ProcessLookupError(errno.ESRCH, 'ordinary service call')
            return self.read_text(self.proc/relative, *args, **kwargs)
        with patch.object(Path, 'read_text', read), self.assertRaises(ProcessLookupError):
            m.process(42)

    def test_status_esrch_discards_only_after_independent_absence(self):
        self.fail_read(filename='status', action=lambda: self.remove_target(43))
        self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        row = self.proof.discarded_processes[0]
        self.assertEqual(row['operation'], 'status')
        self.assertEqual(row['target'], {'pid': 43, 'start_ticks': 10})

    def test_thread_first_stat_esrch_discards_unfinished_sample(self):
        self.fail_read(filename='stat', action=lambda: self.remove_target(43))
        self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        self.assertEqual(self.proof.discarded_processes[0]['target'], {'pid': 43})

    def test_leader_initial_stat_esrch_can_only_admit_absent_owner(self):
        self.fail_read(42, filename='stat', action=lambda: (self.remove_target(43), self.remove_target(42)))
        self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        row = self.proof.discarded_processes[0]
        self.assertIsNone(row['owner_start_ticks'])
        self.assertEqual(row['owner_after'], 'absent')

    def test_status_esrch_present_target_is_fatal(self):
        self.fail_read(filename='status')
        self.assert_fatal()

    def test_stat_esrch_present_target_is_fatal(self):
        self.fail_read(filename='stat')
        self.assert_fatal()

    def test_cgroup_esrch_present_target_is_fatal(self):
        self.fail_read(filename='cgroup')
        self.assert_fatal()

    def test_cgroup_permission_error_is_not_exit(self):
        self.fail_read(filename='cgroup', code=errno.EACCES, action=lambda: self.remove_target(43))
        self.assert_fatal(PermissionError)

    def test_status_io_error_is_not_exit(self):
        self.fail_read(filename='status', code=errno.EIO, action=lambda: self.remove_target(43))
        self.assert_fatal(OSError)

    def test_wrong_mask_precedes_cgroup_esrch(self):
        p = self.proc/'43/status'
        p.write_text(p.read_text().replace('72-75', '0-3'))
        self.fail_read(filename='cgroup', action=lambda: self.remove_target(43))
        self.assert_fatal()
        self.assertEqual(self.failing_reads, 0)

    def test_owner_task_stat_esrch_is_bound_to_target_thread(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == self.proc/'42/task/43/stat':
                self.remove_target(43)
                raise ProcessLookupError(errno.ESRCH, 'read after exit')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        row = self.proof.discarded_processes[0]
        self.assertEqual(row['target']['pid'], 43)
        self.assertEqual(row['path'], str(self.proc/'42/task/43/stat'))

    def test_owner_exit_during_disappearance_check_is_recorded(self):
        self.fail_read(filename='cgroup', action=lambda: self.remove_target(43))
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == self.proc/'42/cgroup' and not (self.proc/'43').exists():
                self.remove_target(42)
                raise ProcessLookupError(errno.ESRCH, 'owner exited during check')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        self.assertEqual(self.proof.discarded_processes[0]['owner_after'], 'absent')

    def test_new_exit_paths_keep_full_sandbox_id_coverage_required(self):
        self.fail_read(filename='status', action=lambda: self.remove_target(43))
        self.proof.sample()
        fanout = self.base/'fanout.json'
        fanout.write_text(json.dumps([{'children': [{'sandbox_id': 'sandbox1'}]}]))
        with self.assertRaisesRegex(RuntimeError, 'Missing placement proof'):
            self.proof.verify_ids(fanout)

    def test_observer_own_esrch_still_fails_with_location(self):
        from unittest.mock import Mock
        self.proof.observer = Mock(receipt={})
        self.proof.observer.check.side_effect = ProcessLookupError(errno.ESRCH, 'observer itself')
        self.proof.watch()
        self.assertEqual(len(self.proof.errors), 1)
        self.assertEqual(self.proof.discarded_processes, [])
        detail = self.proof.observer.receipt['error_context']
        self.assertEqual(detail['phase'], 'observer-before-sample')
        self.assertEqual(detail['errno'], errno.ESRCH)
        self.assertTrue(any(frame['function'] == 'watch' for frame in detail['frames']))


if __name__ == '__main__':
    unittest.main()
