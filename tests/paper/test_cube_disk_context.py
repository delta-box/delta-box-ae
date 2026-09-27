import copy
from contextlib import ExitStack
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae'))
from runners import cube_disk as disk
spec = importlib.util.spec_from_file_location('cube_disk_context', ROOT / 'ae/scripts/cube_disk_context.py')
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)


def physical(path):
    return {'path': str(path), 'device': 42,
            'mount': {'target': '/selected', 'source': '/dev/testdisk1',
                      'fstype': 'ext4', 'maj:min': '8:1', 'options': 'rw'},
            'physical_disks': [{'path': '/dev/testdisk', 'maj:min': '8:0',
                               'size': 1000, 'model': 'test', 'serial': 'fixed'}],
            'lsblk': {'blockdevices': []}}


class CompatibleCase(unittest.TestCase):
    def enterContext(self, manager):
        if not hasattr(self, '_contexts'):
            self._contexts = ExitStack()
            self.addCleanup(self._contexts.close)
        return self._contexts.enter_context(manager)


class ServiceFixture:
    """Exercise the real context state machine, copying actual temporary files."""
    def __init__(self, test):
        self.test = test
        self.root = Path(test.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.source = self.root / 'original.xfs'
        self.source.write_bytes(b'original image contents' * 128)
        self.source_before = self.source.read_bytes()
        self.storage = self.root / 'original-storage'
        self.storage.mkdir()
        self.paths = []
        for i in range(6):
            path = self.root / f'original-{i}'
            path.mkdir()
            (path / 'keep').write_text(f'original-{i}')
            self.paths.append(str(path))
        self.cgroup = self.root / 'cgroup'
        self.cgroup.mkdir()
        (self.cgroup / 'cpuset.cpus').write_text('0-3\n')
        (self.cgroup / 'cpuset.mems').write_text('0\n')
        self.drop = self.root / 'unit' / 'disk.conf'
        self.drop.parent.mkdir()
        self.lease = io.StringIO()
        self.out = self.root / 'evidence'
        self.calls = []
        self.active = True
        self.pid = '123'
        self.allowed_cpus, self.allowed_nodes = '', ''
        self.mounted = set()
        self.fail = None
        self.failed = False
        self.inventory_calls = 0
        self.inventory_changes = False
        patches = [
            mock.patch.object(context.os, 'geteuid', return_value=0),
            mock.patch.object(context, 'validate_placement'),
            mock.patch.object(context, 'STORAGE', self.storage),
            mock.patch.object(context, 'PATHS', tuple(self.paths)),
            mock.patch.object(context, 'CGROUP', self.cgroup),
            mock.patch.object(context, 'DROP', self.drop),
            mock.patch.object(context, 'MEMORY_DROP', self.root / 'memory.conf'),
            mock.patch.object(context, 'acquire_lock', return_value=self.lease),
            mock.patch.object(context, 'disk_proof', side_effect=physical),
            mock.patch.object(context, 'require_space', return_value={'available_bytes': 100 * disk.GIB}),
            mock.patch.object(context, 'visible_mount', return_value={
                'source': '/dev/loop-original', 'fstype': 'xfs'}),
            mock.patch.object(context, 'service_property', side_effect=self.property),
            mock.patch.object(context, 'sandboxes', side_effect=self.inventory),
            mock.patch.object(context, 'output', side_effect=self.output),
            mock.patch.object(context, 'run', side_effect=self.run),
            mock.patch.object(context, 'process_start_ticks', return_value='ticks'),
            mock.patch.object(context, 'verify', return_value={'profile': 'paper-disk'}),
            mock.patch.object(context.os.path, 'ismount', side_effect=lambda p: str(p) in self.mounted),
            mock.patch.object(context, 'path_identity', side_effect=self.identity),
        ]
        for patch in patches:
            test.enterContext(patch)

    def identity(self, value):
        text = str(value)
        if text.startswith('/proc/'):
            text = text.split('/root', 1)[1]
        info = Path(text).stat()
        return {'device': info.st_dev, 'inode': info.st_ino}

    def inventory(self):
        self.inventory_calls += 1
        return [{'sandbox': 'someone-else'}] if self.inventory_changes and self.inventory_calls == 2 else []

    def property(self, name):
        return {'ActiveState': 'active' if self.active else 'inactive',
                'MainPID': self.pid, 'AllowedCPUs': self.allowed_cpus,
                'AllowedMemoryNodes': self.allowed_nodes}[name]

    def output(self, *args):
        args = tuple(map(str, args))
        self.calls.append(args)
        if args[:2] == ('systemctl', 'cat'):
            return 'original service unit'
        if args[:4] == ('losetup', '--list', '--json', '--associated'):
            return json.dumps({'loopdevices': []})
        if args[:3] == ('losetup', '--list', '--json'):
            if args[-1] == '/dev/loop-original':
                return json.dumps({'loopdevices': [{'name': '/dev/loop-original', 'back-file': str(self.source)}]})
            return json.dumps({'loopdevices': [{'name': '/dev/loop-private', 'back-file': str(self.private / 'storage.xfs')}]})
        if args[:3] == ('losetup', '--find', '--show'):
            self.private = Path(args[-1]).parent
            return '/dev/loop-private'
        raise AssertionError(args)

    def run(self, *args, **kwargs):
        args = tuple(map(str, args))
        self.calls.append(args)
        if self.fail and not self.failed and self.fail(args):
            self.failed = True
            raise subprocess.CalledProcessError(1, args)
        if args[:2] == ('systemctl', 'stop'):
            self.active = False
        elif args[:2] == ('systemctl', 'start'):
            self.active = True
            self.pid = '456' if self.drop.exists() else '123'
        elif args[:2] == ('systemctl', 'set-property'):
            self.allowed_cpus = next(a.split('=', 1)[1] for a in args if a.startswith('AllowedCPUs='))
            self.allowed_nodes = next(a.split('=', 1)[1] for a in args if a.startswith('AllowedMemoryNodes='))
        elif args[0] == 'cp':
            subprocess.run(args, check=True)
        elif args[0] == 'mount':
            self.mounted.add(args[-1])
        elif args[0] == 'umount':
            self.mounted.discard(args[-1])
        return SimpleNamespace(returncode=0)

    def open(self):
        return context.disk_service(self.out, workspace=self.workspace, node=2,
                                    cpus='48-71', reserve_gib=10)

    def assert_restored(self, *, loop_expected=True):
        self.test.assertTrue(self.lease.closed)
        self.test.assertTrue(self.active)
        self.test.assertEqual(self.pid, '123')
        self.test.assertEqual((self.allowed_cpus, self.allowed_nodes), ('', ''))
        self.test.assertFalse(self.drop.exists())
        self.test.assertFalse(self.mounted)
        self.test.assertEqual(self.source.read_bytes(), self.source_before)
        for i, path in enumerate(self.paths):
            self.test.assertEqual((Path(path) / 'keep').read_text(), f'original-{i}')
        self.test.assertEqual((self.cgroup / 'cpuset.cpus').read_text(), '0-3\n')
        self.test.assertEqual((self.cgroup / 'cpuset.mems').read_text(), '0\n')
        if loop_expected:
            self.test.assertIn(('losetup', '-d', '/dev/loop-private'), self.calls)
        self.test.assertFalse((self.out / 'cleanup-errors.json').exists())


class CubeDiskContextTests(CompatibleCase):
    def test_success_restores_original_and_retains_private_image_evidence(self):
        fixture = ServiceFixture(self)
        with fixture.open() as manifest:
            proof = json.loads(manifest.read_text())
            self.assertEqual(proof['profile'], 'paper-disk')
            self.assertEqual(proof['cpus'], '48-71')
            self.assertEqual(len(proof['bindings']), 7)
            self.assertTrue(fixture.drop.exists())
            self.assertEqual(fixture.pid, '456')
            self.assertEqual((fixture.private / 'storage.xfs').read_bytes(), fixture.source_before)
        fixture.assert_restored()
        self.assertTrue((fixture.private / 'storage.xfs').exists())
        self.assertTrue(json.loads((fixture.out / 'restored.json').read_text())['override_removed'])
        self.assertIn(('fsfreeze', '--unfreeze', str(fixture.storage)), fixture.calls)
        self.assertFalse(any('fallocate' in call for call in fixture.calls))
        proof = json.loads((fixture.out / 'copy-proof.json').read_text())
        self.assertGreater(proof['pre_measurement_flush']['files_fsynced'], 0)
        self.assertGreater(proof['pre_measurement_flush']['directories_fsynced'], 0)

    def test_body_failure_keeps_original_error_and_restores_service(self):
        fixture = ServiceFixture(self)
        with self.assertRaisesRegex(RuntimeError, 'injected measurement failure'):
            with fixture.open():
                raise RuntimeError('injected measurement failure')
        fixture.assert_restored()

    def test_copy_failure_unfreezes_source_and_restarts_original(self):
        fixture = ServiceFixture(self)
        fixture.fail = lambda args: args[0] == 'cp'
        with self.assertRaises(subprocess.CalledProcessError):
            with fixture.open():
                self.fail('must not yield')
        fixture.assert_restored(loop_expected=False)
        self.assertIn(('fsfreeze', '--unfreeze', str(fixture.storage)), fixture.calls)

    def test_unfreeze_failure_is_retried_during_cleanup_before_original_restart(self):
        fixture = ServiceFixture(self)
        fixture.fail = lambda args: args[:2] == ('fsfreeze', '--unfreeze')
        with self.assertRaises(subprocess.CalledProcessError):
            with fixture.open():
                self.fail('must not yield')
        fixture.assert_restored(loop_expected=False)
        self.assertEqual(sum(args[:2] == ('fsfreeze', '--unfreeze') for args in fixture.calls), 2)

    def test_mount_failure_detaches_owned_loop_and_restarts_original(self):
        fixture = ServiceFixture(self)
        fixture.fail = lambda args: args[0] == 'mount'
        with self.assertRaises(subprocess.CalledProcessError):
            with fixture.open():
                self.fail('must not yield')
        fixture.assert_restored()

    def test_loop_created_without_ack_is_discovered_and_detached(self):
        fixture = ServiceFixture(self)
        original_output = fixture.output
        def interrupted_output(*args):
            if args[:3] == ('losetup', '--find', '--show'):
                fixture.private = Path(args[-1]).parent
                raise subprocess.CalledProcessError(1, args)
            if args[:4] == ('losetup', '--list', '--json', '--associated'):
                return json.dumps({'loopdevices': [{
                    'name': '/dev/loop-private', 'back-file': str(fixture.private / 'storage.xfs')}]})
            return original_output(*args)
        with mock.patch.object(context, 'output', side_effect=interrupted_output):
            with self.assertRaises(subprocess.CalledProcessError):
                with fixture.open():
                    self.fail('must not yield')
        fixture.assert_restored()

    def test_start_failure_removes_override_restores_placement_and_detaches_loop(self):
        fixture = ServiceFixture(self)
        fixture.fail = lambda args: args[:2] == ('systemctl', 'start') and fixture.drop.exists()
        with self.assertRaises(subprocess.CalledProcessError):
            with fixture.open():
                self.fail('must not yield')
        fixture.assert_restored()

    def test_inventory_change_after_stop_prevents_copy_and_restores_original(self):
        fixture = ServiceFixture(self)
        fixture.inventory_changes = True
        with self.assertRaisesRegex(ValueError, 'inventory changed'):
            with fixture.open():
                self.fail('must not yield')
        fixture.assert_restored(loop_expected=False)
        self.assertFalse(any(args[0] == 'cp' for args in fixture.calls))

    def test_busy_service_before_stop_closes_lease_without_touching_service(self):
        fixture = ServiceFixture(self)
        with mock.patch.object(context, 'sandboxes', return_value=[{}]):
            with self.assertRaisesRegex(ValueError, 'must be idle'):
                with fixture.open():
                    self.fail('must not yield')
        self.assertTrue(fixture.lease.closed)
        self.assertFalse(any(args[:2] == ('systemctl', 'stop') for args in fixture.calls))

    def test_capacity_failure_closes_lease_before_stop(self):
        fixture = ServiceFixture(self)
        with mock.patch.object(context, 'require_space', side_effect=ValueError('capacity rejected')):
            with self.assertRaisesRegex(ValueError, 'capacity rejected'):
                with fixture.open():
                    self.fail('must not yield')
        self.assertTrue(fixture.lease.closed)
        self.assertFalse(any(args[:2] == ('systemctl', 'stop') for args in fixture.calls))

    def test_failed_post_start_verification_cleans_up_every_owned_resource(self):
        fixture = ServiceFixture(self)
        with mock.patch.object(context, 'verify', side_effect=ValueError('wrong physical disk')):
            with self.assertRaisesRegex(ValueError, 'wrong physical disk'):
                with fixture.open():
                    self.fail('must not yield')
        fixture.assert_restored()

    def test_cleanup_error_is_recorded_and_does_not_mask_measurement_error(self):
        fixture = ServiceFixture(self)
        with self.assertRaisesRegex(RuntimeError, 'measurement failed'):
            with fixture.open():
                fixture.fail = lambda args: args[0] == 'umount'
                raise RuntimeError('measurement failed')
        self.assertTrue(fixture.lease.closed)
        self.assertTrue((fixture.out / 'cleanup-errors.json').exists())
        self.assertTrue(fixture.active)
        self.assertEqual(fixture.pid, '123')

    def test_full_logical_image_size_is_charged_before_sparse_copy(self):
        fixture = ServiceFixture(self)
        with fixture.source.open('r+b') as stream:
            stream.truncate(1 << 26)
        fixture.source_before = fixture.source.read_bytes()
        with mock.patch.object(context, 'require_space', return_value={}) as capacity:
            with fixture.open():
                first = capacity.call_args_list[0].kwargs
                self.assertGreaterEqual(first['allocation_bytes'], 1 << 26)
                self.assertEqual(first['reserve_bytes'], 10 * disk.GIB)
        fixture.assert_restored()

    def test_metadata_hardlinks_xattrs_and_bytes_are_independently_checked(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = root / 'source'
        target = root / 'target'
        source.mkdir()
        (source / 'original').write_bytes(b'bytes')
        os.link(source / 'original', source / 'hardlink')
        (source / 'symlink').symlink_to('original')
        os.setxattr(source / 'original', 'user.test', b'xattr')
        subprocess.run(['cp', '-a', str(source), str(target)], check=True)
        self.assertEqual(context.tree_manifest(source), context.tree_manifest(target))
        (target / 'original').write_bytes(b'other')
        self.assertNotEqual(context.tree_manifest(source), context.tree_manifest(target))

    def test_lock_acquisition_failure_closes_open_descriptor(self):
        handle = io.StringIO()
        with mock.patch.object(context.os, 'open', return_value=123), \
             mock.patch.object(context.os, 'fdopen', return_value=handle), \
             mock.patch.object(context.os, 'fstat', return_value=SimpleNamespace(
                 st_mode=0o100600, st_nlink=1, st_uid=os.geteuid())), \
             mock.patch.object(context.fcntl, 'flock', side_effect=BlockingIOError):
            with self.assertRaises(BlockingIOError):
                context.acquire_lock()
        self.assertTrue(handle.closed)


class CubeDiskVerificationTests(CompatibleCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.private = self.workspace / 'private'
        self.private.mkdir()
        self.image = self.private / 'storage.xfs'
        self.image.write_bytes(b'image')
        self.cgroup = self.root / 'cgroup'
        self.cgroup.mkdir()
        (self.cgroup / 'cpuset.cpus.effective').write_text('48-71')
        (self.cgroup / 'cpuset.mems.effective').write_text('2')
        bindings = {}
        self.targets = [disk.STORAGE, *disk.PATHS]
        for i, target in enumerate(self.targets):
            path = self.private / ('storage' if i == 0 else f'path-{i-1}')
            path.mkdir()
            bindings[target] = {'source': str(path), 'identity': disk.path_identity(path)}
        self.expected = {
            'schema_version': 1, 'profile': 'paper-disk', 'service_pid': 123,
            'service_start_ticks': 'ticks', 'node': 2, 'cpus': '48-71',
            'workspace': str(self.workspace), 'private_root': str(self.private),
            'private_root_identity': disk.path_identity(self.private),
            'workspace_disk': physical(self.workspace), 'bindings': bindings,
            'image_identity': disk.path_identity(self.image),
            'loop': {'name': '/dev/loop-private', 'back-file': str(self.image)},
            'initial_allocated_bytes': disk.allocated_bytes([self.image]),
            'reserve_bytes': 10 * disk.GIB,
        }
        self.expected['identity'] = {'profile': 'paper-disk', 'disk': disk.disk_identity(physical(self.workspace)),
            'workspace_mount_options': 'rw', 'node': 2, 'service_cpus': '48-71'}
        self.manifest = self.root / 'storage.json'
        self.save()
        self.config = {'cube': {'disk_manifest': str(self.manifest), 'service_cpus': '48-71'},
                       'measurement': {'numa_node': 2, 'cpus': '48-51'}}
        self.actual_loop = copy.deepcopy(self.expected['loop'])
        self.actual_status = {'Cpus_allowed_list': '48-71', 'Mems_allowed_list': '2'}
        self.bad_visible_target = None
        self.real_identity = disk.path_identity
        for patch in (
            mock.patch.object(disk, 'output', side_effect=self.output),
            mock.patch.object(disk, 'process_start_ticks', return_value='ticks'),
            mock.patch.object(disk, 'process_status', side_effect=lambda pid: self.actual_status),
            mock.patch.object(disk, 'CGROUP', self.cgroup),
            mock.patch.object(disk, 'disk_proof', side_effect=physical),
            mock.patch.object(disk, 'visible_mount', side_effect=self.mount),
            mock.patch.object(disk, 'path_identity', side_effect=self.identity),
            mock.patch.object(disk, 'require_space', return_value={'available_bytes': 20 * disk.GIB}),
        ):
            self.enterContext(patch)

    def save(self):
        self.manifest.write_text(json.dumps(self.expected))

    def output(self, *args):
        if args[0] == 'systemctl':
            return '123'
        if args[0] == 'losetup':
            return json.dumps({'loopdevices': [self.actual_loop]})
        raise AssertionError(args)

    def identity(self, value):
        value = str(value)
        if value.startswith('/proc/'):
            target = value.split('/root', 1)[1]
            found = dict(self.expected['bindings'][target]['identity'])
            if target == self.bad_visible_target:
                found['inode'] += 1
            return found
        return self.real_identity(value)

    def mount(self, target, pid=None):
        return {'target': str(target), 'source': '/dev/loop-private' if str(target) == disk.STORAGE else '/dev/testdisk1',
                'fstype': 'xfs' if str(target) == disk.STORAGE else 'ext4', 'maj:min': '8:1', 'options': 'rw'}

    def test_distinct_service_and_runner_cpu_sets_are_recorded(self):
        result = disk.verify(self.config)
        self.assertEqual(result['service_cpus'], '48-71')
        self.assertEqual(result['runner_cpus'], '48-51')
        self.assertEqual(len(result['paths']), 7)

    def test_stable_identity_excludes_private_attempt_fields(self):
        result = disk.verify(self.config)
        self.assertEqual(result['identity'], self.expected['identity'])
        self.assertNotIn('service_pid', result['identity'])
        self.assertNotIn('private_root', result['identity'])
        self.assertNotIn('loop', result['identity'])

    def test_stale_pid_start_time_is_rejected(self):
        with mock.patch.object(disk, 'process_start_ticks', return_value='reused-pid'):
            with self.assertRaisesRegex(ValueError, 'identity or placement changed'):
                disk.verify(self.config)

    def test_wrong_daemon_affinity_is_rejected(self):
        self.actual_status['Cpus_allowed_list'] = '48-51'
        with self.assertRaisesRegex(ValueError, 'daemon affinity'):
            disk.verify(self.config)

    def test_wrong_sandbox_cgroup_is_rejected(self):
        (self.cgroup / 'cpuset.mems.effective').write_text('0-3')
        with self.assertRaisesRegex(ValueError, 'cgroup'):
            disk.verify(self.config)

    def test_shared_device_but_wrong_bound_directory_is_rejected(self):
        self.bad_visible_target = disk.PATHS[0]
        with self.assertRaisesRegex(ValueError, 'binding identity differs'):
            disk.verify(self.config)

    def test_missing_one_private_binding_is_rejected(self):
        del self.expected['bindings'][disk.PATHS[-1]]
        self.save()
        with self.assertRaisesRegex(ValueError, 'all seven'):
            disk.verify(self.config)

    def test_loop_backing_retarget_is_rejected(self):
        self.actual_loop['back-file'] = str(self.root / 'other.xfs')
        with self.assertRaisesRegex(ValueError, 'loop or backing-file identity'):
            disk.verify(self.config)

    def test_changed_physical_device_is_rejected(self):
        value = physical(self.workspace)
        value['physical_disks'][0]['serial'] = 'different'
        with mock.patch.object(disk, 'disk_proof', return_value=value):
            with self.assertRaisesRegex(ValueError, 'physical disk identity changed'):
                disk.verify(self.config)

    def test_capacity_is_checked_again_per_input(self):
        with mock.patch.object(disk, 'require_space', side_effect=ValueError('space exhausted')):
            with self.assertRaisesRegex(ValueError, 'space exhausted'):
                disk.verify(self.config)


class CubeDiskAdmissionTests(CompatibleCase):
    def test_tmpfs_overlay_and_network_are_rejected_before_lsblk(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for fstype, source in [('tmpfs', 'tmpfs'), ('overlay', 'overlay'), ('nfs', 'host:/path')]:
            with self.subTest(fstype=fstype), \
                 mock.patch.object(disk, 'visible_mount', return_value={
                     'fstype': fstype, 'source': source}), \
                 mock.patch.object(disk, 'output') as execute:
                with self.assertRaisesRegex(ValueError, 'physical disk'):
                    disk.disk_proof(root)
                execute.assert_not_called()

    def test_no_proven_physical_ancestor_or_loop_workspace_is_rejected(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for devices in ([{'type': 'lvm'}], [{'type': 'loop'}, {'type': 'disk'}]):
            with self.subTest(devices=devices), \
                 mock.patch.object(disk, 'visible_mount', return_value={
                     'fstype': 'xfs', 'source': '/dev/whatever'}), \
                 mock.patch.object(disk, 'output', return_value=json.dumps({'blockdevices': devices})):
                with self.assertRaises(ValueError):
                    disk.disk_proof(root)

    def test_capacity_uses_available_space_and_never_lowers_ten_gib_reserve(self):
        with mock.patch.object(disk.os, 'statvfs', return_value=SimpleNamespace(
                f_bavail=11 * disk.GIB, f_frsize=1)):
            with self.assertRaisesRegex(ValueError, 'capacity'):
                disk.require_space('/selected', reserve_bytes=10 * disk.GIB, allocation_bytes=2 * disk.GIB)
            with self.assertRaisesRegex(ValueError, 'at least 10'):
                disk.require_space('/selected', reserve_bytes=9 * disk.GIB)
            result = disk.require_space('/selected', reserve_bytes=10 * disk.GIB)
            self.assertEqual(result['available_bytes'], 11 * disk.GIB)


if __name__ == '__main__':
    unittest.main()
