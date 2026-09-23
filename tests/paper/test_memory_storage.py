"""RAM staging fails closed and releases its owned mount on partial failures."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'replay'))
from memory_storage import memory_images, memory_budget, readonly_image_capacity
from provenance import file_digest


class MemoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'ram'
        self.runtime.mkdir()
        self.config = {'images': {}, 'mem_mib': 8192}
        budget_patch = patch('memory_storage.memory_budget', return_value={'available_bytes': 1 << 40, 'required_bytes': 1 << 30})
        budget_patch.start()
        self.addCleanup(budget_patch.stop)
        for key in ('base_xfs', 'data_xfs'):
            source = self.root / key
            source.write_bytes((key + 'fixture').encode())
            self.config[key] = str(source)
            self.config['images'][key] = file_digest(source)
        self.calls = []

    def command(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == 'cp':
            Path(args[-1]).write_bytes(Path(args[-2]).read_bytes())
        return subprocess.CompletedProcess(args, 0)

    def output(self, args, **kwargs):
        if args[0] == 'numactl':
            return 'policy: bind\nmembind: 2\n'
        return json.dumps({'filesystems': [{'target': str(self.runtime), 'fstype': 'tmpfs', 'options': 'rw,noswap'}]})

    def test_both_images_are_verified_before_use_and_mount_is_released(self):
        with patch('memory_storage.subprocess.run', side_effect=self.command), patch('memory_storage.subprocess.check_output', side_effect=self.output):
            with memory_images(self.runtime, self.config, self.root / 'storage.json') as (rootfs, data):
                self.assertEqual(rootfs.parent, self.runtime)
                self.assertEqual(rootfs.name, "base.xfs")
                self.assertFalse((self.runtime / "rootfs.xfs").exists(), "VM startup requires a fresh writable path")
                self.assertEqual(data.read_bytes(), Path(self.config['data_xfs']).read_bytes())
                record = json.loads((self.root / 'storage.json').read_text())
                self.assertEqual(record['disks']['data_xfs']['sha256'], self.config['images']['data_xfs']['sha256'])
                self.assertIn('noswap', record['mount']['options'])
        self.assertEqual(self.calls[-1], ['umount', str(self.runtime)])

    def test_failed_mount_never_copies_or_yields_a_disk_fallback(self):
        with patch('memory_storage.subprocess.check_output', side_effect=self.output), patch('memory_storage.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['mount'])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                with memory_images(self.runtime, self.config, self.root / 'storage.json'):
                    self.fail('mount failure must never start a VM')
        self.assertEqual(run.call_count, 1)

    def test_corrupted_copy_fails_before_guest_and_unmounts(self):
        def corrupt(args, **kwargs):
            self.command(args, **kwargs)
            if args[0] == 'cp': Path(args[-1]).write_bytes(b'corruption')
        with patch('memory_storage.subprocess.run', side_effect=corrupt), patch('memory_storage.subprocess.check_output', side_effect=self.output):
            with self.assertRaisesRegex(RuntimeError, 'differs'):
                with memory_images(self.runtime, self.config, self.root / 'storage.json'):
                    self.fail('corrupted image must never boot')
        self.assertEqual(self.calls[-1], ['umount', str(self.runtime)])

    def test_low_bound_memory_fails_before_allocating_or_mounting(self):
        with patch('memory_storage.memory_budget', return_value={'available_bytes': 1 << 30, 'required_bytes': 20 << 30}), \
             patch('memory_storage.subprocess.check_output', side_effect=self.output), \
             patch('memory_storage.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Insufficient free memory'):
                with memory_images(self.runtime, self.config, self.root / 'storage.json'):
                    self.fail('must not start a guest under NUMA memory pressure')
        run.assert_not_called()
        self.assertEqual(json.loads((self.root / 'storage.json').read_text())['status'], 'insufficient-numa-memory')

    def test_low_memory_after_staging_prevents_vm_and_releases_mount(self):
        budgets = [{'available_bytes': 100 << 30, 'required_bytes': 20 << 30},
                   {'available_bytes': 1 << 30, 'required_bytes': 10 << 30}]
        with patch('memory_storage.memory_budget', side_effect=budgets), \
             patch('memory_storage.subprocess.run', side_effect=self.command), \
             patch('memory_storage.subprocess.check_output', side_effect=self.output):
            with self.assertRaisesRegex(RuntimeError, 'after RAM staging'):
                with memory_images(self.runtime, self.config, self.root / 'storage.json'):
                    self.fail('guest cannot start without full RAM reserve')
        self.assertEqual(self.calls[-1], ['umount', str(self.runtime)])
        self.assertEqual(json.loads((self.root / 'storage.json').read_text())['status'], 'insufficient-numa-memory-after-staging')

    def test_staged_budget_accounts_for_guest_and_future_rootfs_growth(self):
        nodes = self.root / 'nodes'
        (nodes / 'node2').mkdir(parents=True)
        (nodes / 'node2/meminfo').write_text('Node 2 MemFree: 41943040 kB\n')
        base = self.runtime / 'base.xfs'
        with base.open('wb') as f:
            f.truncate(3 << 30)
        sources = {k: Path(self.config[k]) for k in ('base_xfs', 'data_xfs')}
        config = memory_budget(self.config, sources, 'policy: bind\nmembind: 2\n', nodes, staged_base=base)
        self.assertEqual(config['required_bytes'], 13 << 30)
        self.assertEqual(config['rootfs_growth_bytes'], 3 << 30)
        self.assertEqual(config['phase'], 'guest-boot')

    def test_capacity_counts_only_bound_nodes_and_includes_guest_ram(self):
        nodes = self.root / 'nodes'
        (nodes / 'node2').mkdir(parents=True)
        (nodes / 'node2/meminfo').write_text('Node 2 MemFree:       41943040 kB\nNode 2 HugePages_Total: 0\nNode 2 HugePages_Free: 0\n')
        sources = {k: Path(self.config[k]) for k in ('base_xfs', 'data_xfs')}
        budget = memory_budget(self.config, sources, 'policy: bind\nmembind: 2\n', nodes)
        self.assertEqual(budget['available_bytes'], 40 << 30)
        self.assertGreater(budget['required_bytes'], 10 << 30)
        (nodes / 'node2/meminfo').write_text('Node 2 MemFree: 1048576 kB\nNode 2 Inactive(file): 4194304 kB\nNode 2 Dirty: 1048576 kB\nNode 2 Writeback: 0 kB\nNode 2 Mapped: 1048576 kB\nNode 2 Shmem: 67108864 kB\n')
        budget = memory_budget(self.config, sources, 'policy: bind\nmembind: 2\n', nodes)
        self.assertEqual(budget['available_bytes'], 3 << 30)
        self.assertEqual(budget['reclaimable_file_bytes'], {'2': 2 << 30})
        # Re-reading our immutable source images promotes them to active file
        # LRU without making their clean, unmapped cache non-reclaimable.
        with (nodes / 'node2/meminfo').open('a') as f:
            f.write('Node 2 Active(file): 2097152 kB\n')
        active_budget = memory_budget(self.config, sources, 'policy: bind\nmembind: 2\n', nodes)
        self.assertEqual(active_budget['available_bytes'], 5 << 30)
        self.assertEqual(active_budget['reclaimable_file_bytes'], {'2': 4 << 30})
        with self.assertRaisesRegex(RuntimeError, 'explicit NUMA'):
            memory_budget(self.config, sources, 'policy: default\nmembind: 0 1 2 3\n', nodes)

    def test_disposable_base_becomes_fresh_rootfs_without_second_allocation(self):
        import importlib.util
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location('ram_staging_vm', Path(__file__).resolve().parents[2] / 'ae/runners/vm.py')
        vm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vm)
        base = self.runtime / 'base.xfs'
        base.write_bytes(b'verified base')
        inode = base.stat().st_ino
        args = SimpleNamespace(socket=self.runtime/'fc.sock', run_rootfs=self.runtime/'rootfs.xfs',
                               base_xfs=base, consume_staged_base=True, reuse_rootfs=False)
        vm.prepare_rootfs(args)
        self.assertEqual(args.run_rootfs.read_bytes(), b'verified base')
        self.assertEqual(args.run_rootfs.stat().st_ino, inode)
        self.assertFalse(base.exists())
        # Shared source images are never eligible for the consume operation.
        args.run_rootfs.unlink()
        args.base_xfs = Path(self.config['base_xfs'])
        with self.assertRaisesRegex(ValueError, 'private runtime'):
            vm.prepare_rootfs(args)
        self.assertTrue(args.base_xfs.exists())


class ReadonlyCapacityTests(unittest.TestCase):
    def test_real_sparse_image_counts_resident_extents_but_preserves_logical_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.xfs'
            with path.open('wb') as stream:
                stream.write(b'a' * 4096)
                stream.seek(16 << 20)
                stream.write(b'z' * 4096)
            result = readonly_image_capacity(path)
            self.assertEqual(result['method'], 'page-rounded-seek-data-extents')
            self.assertGreaterEqual(result['bytes'], 8192)
            self.assertLess(result['bytes'], path.stat().st_size)
            self.assertEqual(path.stat().st_size, (16 << 20) + 4096)

    def test_unknown_extent_support_reserves_full_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.xfs'
            path.write_bytes(b'a' * 5000)
            with patch('memory_storage.os.lseek', side_effect=OSError(errno.EINVAL, 'unsupported')):
                self.assertEqual(readonly_image_capacity(path), dict(bytes=8192, method='logical-size'))

    def test_page_shared_by_two_extents_is_counted_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.xfs'
            path.write_bytes(b'a' * 8192)
            with patch('memory_storage.os.lseek', side_effect=[0, 100, 200, 5000, OSError(errno.ENXIO, 'end')]):
                self.assertEqual(readonly_image_capacity(path)['bytes'], 8192)
