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
            mock.patch.object(context, 'master_idle_readiness', side_effect=self.readiness),
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

    def readiness(self, pid, evidence, **kwargs):
        self.inventory_calls += 1
        if self.inventory_changes and self.inventory_calls == 2:
            raise ValueError('Cube must be idle: activity appeared before stop')
        if not self.active or str(pid) != self.pid:
            raise AssertionError('readiness must only run against active current service')
        return {'ready': True, 'idle': True, 'service_pid': pid}


    def property(self, name):
        return {'ActiveState': 'active' if self.active else 'inactive',
                'MainPID': self.pid if self.active else '0', 'AllowedCPUs': self.allowed_cpus,
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

    def test_inventory_change_before_stop_prevents_copy_without_stopping_original(self):
        fixture = ServiceFixture(self)
        fixture.inventory_changes = True
        with self.assertRaisesRegex(ValueError, 'activity appeared'):
            with fixture.open():
                self.fail('must not yield')
        fixture.assert_restored(loop_expected=False)
        self.assertFalse(any(args[0] == 'cp' or args[:2] == ('systemctl', 'stop') for args in fixture.calls))

    def test_busy_service_before_stop_closes_lease_without_touching_service(self):
        fixture = ServiceFixture(self)
        with mock.patch.object(context, 'master_idle_readiness', side_effect=ValueError('Cube must be idle')):
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

    def test_private_rpc_readiness_failure_restores_original_and_owned_mounts(self):
        fixture = ServiceFixture(self)
        original = fixture.readiness
        def readiness(pid, evidence, **kwargs):
            if fixture.drop.exists():
                raise TimeoutError('cached Master channel unavailable')
            return original(pid, evidence, **kwargs)
        with mock.patch.object(context, 'master_idle_readiness', side_effect=readiness):
            with self.assertRaisesRegex(TimeoutError, 'cached Master'):
                with fixture.open():
                    self.fail('must not yield before real RPC succeeds')
        fixture.assert_restored()

    def test_original_restore_rpc_failure_is_recorded_and_prevents_clean_exit(self):
        fixture = ServiceFixture(self)
        original = fixture.readiness
        def readiness(pid, evidence, **kwargs):
            if Path(evidence).name == 'restored-readiness.json':
                raise TimeoutError('original Master channel unavailable')
            return original(pid, evidence, **kwargs)
        with mock.patch.object(context, 'master_idle_readiness', side_effect=readiness):
            with self.assertRaisesRegex(RuntimeError, 'cleanup/restoration failed'):
                with fixture.open():
                    pass
        self.assertTrue(fixture.lease.closed)
        self.assertTrue(fixture.active)
        self.assertFalse(fixture.drop.exists())
        self.assertFalse(fixture.mounted)
        self.assertTrue((fixture.out / 'cleanup-errors.json').exists())

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




class CubeMasterReadinessTests(CompatibleCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.evidence = self.root / 'ready.json'
        self.urls = []
        self.clock = 0.0
        self.responses = []
        self.enterContext(mock.patch.object(context, 'service_property',
            side_effect=lambda name: '123' if name == 'MainPID' else 'active'))
        self.enterContext(mock.patch.object(context, 'process_start_ticks', return_value='stable'))
        self.enterContext(mock.patch.object(context.time, 'monotonic', side_effect=lambda: self.clock))
        self.enterContext(mock.patch.object(context.time, 'sleep', side_effect=self.sleep))
        self.enterContext(mock.patch.object(context.urllib.request, 'urlopen', side_effect=self.urlopen))

    def sleep(self, seconds):
        self.clock += seconds

    def urlopen(self, url, **kwargs):
        self.urls.append(url)
        if not self.responses:
            raise AssertionError('unexpected extra readiness read')
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        request_id = context.urllib.parse.parse_qs(context.urllib.parse.urlsplit(url).query)['requestID'][0]
        value = {'requestID': request_id, 'ret': {'ret_code': response}} if type(response) is int else response
        return io.StringIO(json.dumps(value))

    def test_cached_rpc_failure_then_actual_empty_success_only_repeats_reads(self):
        self.responses = [130595, 130406]
        proof = context.master_idle_readiness(123, self.evidence, timeout=1)
        self.assertTrue(proof['ready'])
        self.assertEqual([r['ret_code'] for r in proof['attempts']], [130595, 130406])
        self.assertEqual(len(set(self.urls)), 2)
        for url in self.urls:
            parsed = context.urllib.parse.urlsplit(url)
            self.assertEqual(parsed.path, '/cube/sandbox/info')
            self.assertEqual(set(context.urllib.parse.parse_qs(parsed.query)), {'host_id', 'requestID'})
            self.assertEqual(context.urllib.parse.parse_qs(parsed.query)['host_id'], ['198.18.0.1'])

    def test_http_success_alone_is_not_rpc_success_or_idle(self):
        self.responses = [200]
        with self.assertRaisesRegex(ValueError, 'must be idle'):
            context.master_idle_readiness(123, self.evidence)
        self.assertFalse(json.loads(self.evidence.read_text())['ready'])
        self.assertEqual(len(self.urls), 1)

    def test_wrong_request_identity_and_invalid_error_code_fail_closed(self):
        for value in [
            {'requestID': 'other', 'ret': {'ret_code': 130406}},
            {'requestID': 'other', 'ret': {'ret_code': '130406'}},
            [],
            130401,
        ]:
            with self.subTest(value=value):
                self.responses = [value]
                with self.assertRaises(ValueError):
                    context.master_idle_readiness(123, self.evidence)
                self.assertFalse(json.loads(self.evidence.read_text())['ready'])

    def test_success_code_with_nonempty_data_is_rejected(self):
        def contradictory(url, **kwargs):
            request_id = context.urllib.parse.parse_qs(context.urllib.parse.urlsplit(url).query)['requestID'][0]
            return io.StringIO(json.dumps({'requestID': request_id, 'ret': {'ret_code': 130406},
                                          'data': [{'sandbox_id': 'busy'}]}))
        with mock.patch.object(context.urllib.request, 'urlopen', side_effect=contradictory):
            with self.assertRaisesRegex(ValueError, 'must be idle'):
                context.master_idle_readiness(123, self.evidence)

    def test_bound_boolean_code_and_success_with_empty_data_are_rejected(self):
        for code in (True, 200):
            def invalid(url, **kwargs):
                request_id = context.urllib.parse.parse_qs(context.urllib.parse.urlsplit(url).query)['requestID'][0]
                return io.StringIO(json.dumps({'requestID': request_id, 'ret': {'ret_code': code}, 'data': []}))
            with self.subTest(code=code), mock.patch.object(context.urllib.request, 'urlopen', side_effect=invalid):
                with self.assertRaises(ValueError):
                    context.master_idle_readiness(123, self.evidence)

    def test_authentication_error_is_not_retried_as_transient_readiness(self):
        self.responses = [context.urllib.error.HTTPError('local', 403, 'denied', {}, None)]
        with self.assertRaisesRegex(ValueError, 'HTTP request rejected: 403'):
            context.master_idle_readiness(123, self.evidence)
        self.assertEqual(len(self.urls), 1)
        self.assertEqual(json.loads(self.evidence.read_text())['attempts'][0]['http_status'], 403)

    def test_service_restart_during_call_is_rejected(self):
        self.responses = [130406]
        with mock.patch.object(context, 'process_start_ticks', side_effect=['stable', 'stable', 'restarted']):
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                context.master_idle_readiness(123, self.evidence)

    def test_timeout_preserves_failed_rpc_evidence_without_accepting_empty_response(self):
        self.responses = [130595, 130595]
        with self.assertRaisesRegex(TimeoutError, 'did not become ready'):
            context.master_idle_readiness(123, self.evidence, timeout=0.5)
        proof = json.loads(self.evidence.read_text())
        self.assertFalse(proof['ready'])
        self.assertEqual(len(proof['attempts']), 2)

    def test_read_transport_failure_may_recover_before_deadline(self):
        self.responses = [TimeoutError('read timeout'), 130406]
        proof = context.master_idle_readiness(123, self.evidence, timeout=1)
        self.assertTrue(proof['ready'])
        self.assertEqual(proof['attempts'][0]['transport_error_type'], 'TimeoutError')


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
