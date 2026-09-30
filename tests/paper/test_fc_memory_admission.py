import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


capacity = module(ROOT / 'ae/vendor/finalbench/fc_diff_dm/fc_capacity.py',
                  '_fc_memory_capacity_test')
GIB = capacity.GIB


class CapacityTests(unittest.TestCase):
    def check(self, folder, filesystem_free, node_free, **kwargs):
        usage = SimpleNamespace(total=30 * GIB, used=30 * GIB - filesystem_free,
                                free=filesystem_free)
        with patch.object(capacity.shutil, 'disk_usage', return_value=usage), \
                patch.object(capacity, 'node_available', return_value=node_free) as available:
            row = capacity.check_capacity(Path(folder), 'before-staging', 34 * GIB,
                Path(folder) / 'capacity.jsonl', node=3, **kwargs)
        available.assert_called_once_with(3)
        return row

    def test_30_gib_filesystem_and_36_gib_node_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            row = self.check(folder, 30 * GIB, 36 * GIB,
                             filesystem_required=28 * GIB)
            self.assertEqual(row['filesystem_required_bytes'], 28 * GIB)
            self.assertEqual(row['node_required_bytes'], 36 * GIB)
            self.assertEqual(row['numa_node'], 3)
            self.assertEqual(json.loads((Path(folder) / 'capacity.jsonl').read_text()), row)

    def test_filesystem_one_byte_short_refuses_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, 'tmpfs'):
                self.check(folder, 28 * GIB - 1, 36 * GIB,
                           filesystem_required=28 * GIB)
            self.assertEqual(json.loads((Path(folder) / 'capacity.jsonl').read_text())
                             ['tmpfs_free_bytes'], 28 * GIB - 1)

    def test_node_one_byte_short_refuses_despite_sufficient_filesystem(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, 'NUMA 3'):
                self.check(folder, 30 * GIB, 36 * GIB - 1,
                           filesystem_required=28 * GIB)
            self.assertEqual(json.loads((Path(folder) / 'capacity.jsonl').read_text())
                             ['node_required_bytes'], 36 * GIB)

    def test_existing_calls_still_require_same_filesystem_and_node_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, 'tmpfs'):
                self.check(folder, 30 * GIB, 36 * GIB)
            row = self.check(folder, 36 * GIB, 36 * GIB)
            self.assertEqual(row['filesystem_required_bytes'], 36 * GIB)
            self.assertEqual(row['node_required_bytes'], 36 * GIB)

    def test_invalid_explicit_filesystem_budget_refuses(self):
        with tempfile.TemporaryDirectory() as folder:
            for value in (-1, True, '28'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.check(folder, 30 * GIB, 36 * GIB, filesystem_required=value)


class WrapperTests(unittest.TestCase):
    def run_wrapper(self, folder, node_free, initial_identity=None):
        # Reuse the mount-layer fixture rather than mocking the old ExitStack.
        lifecycle = module(ROOT / 'tests/paper/test_memory_job_lifecycle.py',
                           '_fc_memory_lifecycle_fixture')
        fixture = lifecycle.MemoryJobLifecycleTests()
        fixture.setUp()
        wrapper = lifecycle.job
        written = []
        original_write = wrapper.write_json
        def recording_write(path, value):
            written.append((path, dict(value)))
            original_write(path, value)
        environment = ({'AE_MEASUREMENT_IDENTITY': json.dumps(initial_identity)}
                       if initial_identity is not None else {})
        usage = SimpleNamespace(total=30 * GIB, used=0, free=30 * GIB)
        try:
            with patch.object(wrapper, 'check_capacity', side_effect=capacity.check_capacity), \
                    patch.object(wrapper, 'write_json', side_effect=recording_write), \
                    patch.object(capacity.shutil, 'disk_usage', return_value=usage), \
                    patch.object(capacity, 'node_available', return_value=node_free) as available, \
                    patch.dict(os.environ, environment, clear=True):
                if node_free < 36 * GIB:
                    with self.assertRaisesRegex(RuntimeError, 'NUMA 3'):
                        wrapper.run(fixture.args())
                    wrapper.subprocess.Popen.assert_not_called()
                    self.assertFalse(any(row.get('status') == 'running' for _, row in written))
                    result = None
                else:
                    result = wrapper.run(fixture.args())
                    env = wrapper.subprocess.Popen.call_args.kwargs['env']
                    backing = json.loads(env['AE_MEMORY_JOB'])
                    expected_policy = (initial_identity or {}).get('frequency_policy') or 'maximum-pstate'
                    self.assertEqual(backing['frequency_policy'], expected_policy)
                    measured_identity = json.loads(env['AE_MEASUREMENT_IDENTITY'])
                    self.assertEqual(measured_identity['frequency_policy'], expected_policy)
                    self.assertEqual(measured_identity['node'], 3)
                    self.assertEqual(measured_identity['cpus'], [72, 73, 74, 75])
                    self.assertEqual(measured_identity['storage_mode'], 'tmpfs-noswap')
                    for _, row in written:
                        self.assertEqual(row['frequency_policy'], expected_policy)
                    self.assertEqual({row.get('status') for _, row in written if row.get('status')},
                                     {'preparing', 'running', 'archiving', 'archived'})
                    self.assertEqual(backing['admission']['node_required_bytes'], 36 * GIB)
                    self.assertEqual(backing['admission']['filesystem_required_bytes'], 28 * GIB)
                    self.assertEqual(backing['node'], 3)
                    self.assertEqual(backing['cpus'], [72, 73, 74, 75])
                    self.assertEqual(wrapper.subprocess.Popen.call_args.args[0], fixture.args().command)
                    self.assertTrue(wrapper.subprocess.Popen.call_args.kwargs['start_new_session'])
                available.assert_called_once_with(3)
                self.assertIn('ram-mount', fixture.events)  # Fixture asserts exact noswap/size/mpol command.
                self.assertIn('umount:' + str(fixture.suite), fixture.events)
            return result
        finally:
            fixture.doCleanups()

    def test_complete_producer_invocation_uses_30_gib_noswap_and_correct_node(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(self.run_wrapper(folder, 36 * GIB), 0)

    def test_insufficient_node_capacity_prevents_producer_and_unmounts(self):
        with tempfile.TemporaryDirectory() as folder:
            self.run_wrapper(folder, 36 * GIB - 1)

    def test_locked_frequency_preserved_in_child_and_all_archive_records(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(self.run_wrapper(folder, 36 * GIB, initial_identity={
                'frequency_policy': 'locked-2101000-khz', 'node': 1,
                'cpus': '28-31', 'storage_mode': 'nvme'}), 0)

    def test_missing_frequency_uses_original_fallback_in_all_records(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(self.run_wrapper(folder, 36 * GIB,
                                             initial_identity={'node': 1}), 0)


if __name__ == '__main__':
    unittest.main()
