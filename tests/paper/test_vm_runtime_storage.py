"""Host-only storage lifecycle checks; no mount, VM, or measurement is started."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'ae'), str(ROOT / 'ae/runners')]
import vm_experiment as runner


class VMRuntimeStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'output'
        self.output.mkdir()
        self.base = self.root / 'base.xfs'
        self.base.write_bytes(b'unchanged shared base')
        self.config = dict(experiment='correctness', base_xfs=str(self.base))
        self.ae = patch.object(runner, 'AE_ROOT', self.root / 'ae')
        self.ae.start()
        self.addCleanup(self.ae.stop)

    def memory_record(self, path, **kwargs):
        return dict(path=str(path), fstype='tmpfs',
                    mount=dict(target=str(path), fstype='tmpfs', options='rw,noswap'))

    def probe(self, fstype, options, mount_fstype=None):
        return patch.object(runner.subprocess, 'check_output', side_effect=[
            fstype + '\n', json.dumps(dict(filesystems=[dict(
                target=str(self.root), fstype=mount_fstype or fstype, options=options)]))])

    def test_correctness_and_figure9_mount_private_noswap_before_yield(self):
        for experiment in ('correctness', 'figure-09'):
            with self.subTest(experiment=experiment):
                config = dict(self.config, experiment=experiment)
                with patch.object(runner.subprocess, 'run') as run, \
                     patch.object(runner, 'require_memory_workdir', side_effect=self.memory_record) as verify:
                    with runner.runtime_directory(config, self.output) as runtime:
                        self.assertEqual(runtime.parent, self.root / 'ae/work')
                        run.assert_called_once_with(['mount', '-t', 'tmpfs', '-o',
                            f'size={self.base.stat().st_size + 1024**3},noswap',
                            'ae-war-vm-memory', str(runtime)], check=True)
                        verify.assert_called_once_with(runtime, experiment=experiment)
                        evidence = json.loads((self.output / 'host-storage.json').read_text())
                        self.assertEqual(evidence, self.memory_record(runtime))
                        (runtime / 'rootfs.xfs').write_bytes(b'private rootfs')
                    self.assertEqual(run.call_args.args[0], ['umount', str(runtime)])
                    self.assertFalse(runtime.exists())
                    self.assertEqual(self.base.read_bytes(), b'unchanged shared base')

    def test_explicit_disk_workdir_is_rejected_before_allocation(self):
        self.config['work_dir'] = str(self.root)
        with self.probe('ext2/ext3', 'rw'), patch.object(runner.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'correctness.*noswap tmpfs'):
                with runner.runtime_directory(self.config, self.output):
                    self.fail('disk workdir must not be yielded')
        run.assert_not_called()
        self.assertEqual(list(self.root.glob('ae-cpu-*')), [])
        self.assertFalse((self.output / 'host-storage.json').exists())

    def test_swappable_tmpfs_workdir_is_rejected(self):
        self.config['work_dir'] = str(self.root)
        with self.probe('tmpfs', 'rw,size=4G'), patch.object(runner.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'noswap tmpfs'):
                with runner.runtime_directory(self.config, self.output):
                    self.fail('swappable workdir must not be yielded')
        run.assert_not_called()

    def test_inconsistent_mount_fstype_is_rejected(self):
        with self.probe('tmpfs', 'rw,noswap', mount_fstype='ext4'):
            with self.assertRaisesRegex(ValueError, 'noswap tmpfs'):
                runner.require_memory_workdir(self.root, experiment='correctness')

    def test_explicit_noswap_parent_is_verified_but_not_unmounted(self):
        self.config['work_dir'] = str(self.root)
        with patch.object(runner, 'require_memory_workdir', side_effect=self.memory_record) as verify, \
             patch.object(runner.subprocess, 'run') as run:
            with runner.runtime_directory(self.config, self.output) as runtime:
                self.assertEqual(runtime.parent, self.root)
                self.assertEqual(verify.call_count, 2)
                self.assertEqual(verify.call_args.args, (runtime,))
            run.assert_not_called()
        self.assertTrue(self.root.is_dir())
        self.assertFalse(runtime.exists())

    def test_mount_failure_does_not_yield_or_record_success(self):
        failure = subprocess.CalledProcessError(1, 'mount')
        with patch.object(runner.subprocess, 'run', side_effect=failure) as run, \
             patch.object(runner, 'require_memory_workdir') as verify:
            with self.assertRaises(subprocess.CalledProcessError):
                with runner.runtime_directory(self.config, self.output):
                    self.fail('failed mount must not be yielded')
        self.assertEqual(run.call_count, 1)
        verify.assert_not_called()
        self.assertEqual(list((self.root / 'ae/work').glob('ae-cpu-*')), [])
        self.assertFalse((self.output / 'host-storage.json').exists())

    def test_post_mount_verification_failure_unmounts_before_cleanup(self):
        with patch.object(runner.subprocess, 'run') as run, \
             patch.object(runner, 'require_memory_workdir', side_effect=ValueError('noswap absent')):
            with self.assertRaisesRegex(ValueError, 'noswap absent'):
                with runner.runtime_directory(self.config, self.output):
                    self.fail('unverified mount must not be yielded')
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ['mount', 'umount'])
        self.assertEqual(list((self.root / 'ae/work').glob('ae-cpu-*')), [])
        self.assertFalse((self.output / 'host-storage.json').exists())

    def test_guest_failure_still_unmounts_and_removes_private_scratch(self):
        with patch.object(runner.subprocess, 'run') as run, \
             patch.object(runner, 'require_memory_workdir', side_effect=self.memory_record):
            with self.assertRaisesRegex(RuntimeError, 'guest failure'):
                with runner.runtime_directory(self.config, self.output) as runtime:
                    (runtime / 'rootfs.xfs').write_bytes(b'private')
                    raise RuntimeError('guest failure')
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ['mount', 'umount'])
        self.assertFalse(runtime.exists())
        self.assertEqual(self.base.read_bytes(), b'unchanged shared base')

    def test_unmount_failure_is_not_a_successful_run(self):
        with patch.object(runner.subprocess, 'run', side_effect=[None, subprocess.CalledProcessError(1, 'umount')]), \
             patch.object(runner, 'require_memory_workdir', side_effect=self.memory_record):
            with self.assertRaises(subprocess.CalledProcessError):
                with runner.runtime_directory(self.config, self.output):
                    pass

    def test_existing_fanout_storage_behavior_is_unchanged(self):
        config = dict(self.config, experiment='figure-08-deltabox', work_dir=str(self.root))
        with patch.object(runner.subprocess, 'run') as run, \
             patch.object(runner, 'require_memory_workdir') as verify:
            with runner.runtime_directory(config, self.output) as runtime:
                self.assertEqual(runtime.parent, self.root)
            run.assert_not_called()
            verify.assert_not_called()
        self.assertFalse((self.output / 'host-storage.json').exists())


if __name__ == '__main__':
    unittest.main()
