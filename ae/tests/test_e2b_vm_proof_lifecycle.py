"""Local-only VM observer lifecycle contracts; no services or real /proc sampling."""
import errno
import importlib.util
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

if __name__ == '__main__':
    unittest.main()
