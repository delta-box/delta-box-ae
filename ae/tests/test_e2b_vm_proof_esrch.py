"""Bound VM /proc ESRCH contracts over temporary proc/cgroup files only."""
import errno
import importlib.util
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

    def test_post_exit_swap_violation_is_fatal(self):
        self.fail_read(action=lambda: (self.remove_target(43), (self.child/'memory.swap.current').write_text('4096')))
        self.assert_fatal()

    def test_default_process_does_not_admit_esrch(self):
        def read(path, *args, **kwargs):
            relative = path.relative_to('/proc')
            if relative.name == 'numa_maps':
                raise ProcessLookupError(errno.ESRCH, 'ordinary service call')
            return self.read_text(self.proc/relative, *args, **kwargs)
        with patch.object(Path, 'read_text', read), self.assertRaises(ProcessLookupError):
            m.process(42)

    def test_status_esrch_present_target_is_fatal(self):
        self.fail_read(filename='status')
        self.assert_fatal()

    def test_stat_esrch_present_target_is_fatal(self):
        self.fail_read(filename='stat')
        self.assert_fatal()

    def test_cgroup_esrch_present_target_is_fatal(self):
        self.fail_read(filename='cgroup')
        self.assert_fatal()

    def test_wrong_mask_precedes_cgroup_esrch(self):
        p = self.proc/'43/status'
        p.write_text(p.read_text().replace('72-75', '0-3'))
        self.fail_read(filename='cgroup', action=lambda: self.remove_target(43))
        self.assert_fatal()
        self.assertEqual(self.failing_reads, 0)

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
