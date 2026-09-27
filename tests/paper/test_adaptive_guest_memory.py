"""Figure 6(b) retains complete standard/adaptive work with equal guest resources."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae'))
sys.path.insert(0, str(ROOT / 'replay'))
from repro import catalog
from memory_storage import memory_budget


def value(command, option):
    return command[command.index(option) + 1]


class AdaptiveGuestMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.config = dict(images_dir='/images', kernel='/kernel', base_xfs='/base',
                           mem_mib=8192, vcpus=4, timeout=14400,
                           checkpoint_profile='runtime-default',
                           vm_storage='tmpfs', prewarm_policy='off')

    def plan(self, experiments=('figure-06-adaptive',), config=None):
        return catalog.build_jobs(experiments, self.config if config is None else config,
                                  self.path / 'config.json', self.path / 'out')

    def test_all_24_jobs_preserve_complete_inputs_and_equal_arm_resources(self):
        before = copy.deepcopy(self.config)
        jobs = self.plan()
        self.assertEqual(len(jobs), 24)
        self.assertEqual(self.config, before)
        pairs = {}
        for job in jobs:
            command = job['command']
            self.assertEqual(value(command, '--mem-mib'), '16384')
            self.assertEqual(value(command, '--vcpus'), '4')
            self.assertEqual(value(command, '--checkpoint-profile'), 'runtime-default')
            self.assertEqual(value(command, '--storage-mode'), 'tmpfs')
            self.assertEqual(value(command, '--prewarm-policy'), 'off')
            self.assertEqual(value(command, '--legacy-timing-policy'), 'paper-zero')
            self.assertNotIn('--max-events', command)
            self.assertNotIn('--memory-policy', command)
            self.assertEqual(job['run_purpose'], 'full-cohort')
            self.assertEqual(job['scheduled_events'],
                             job['scheduled_checkpoints'] + job['scheduled_restores'])
            self.assertEqual(job['recorded_wait_s'], 0)
            self.assertEqual(len(job['inputs']), 1)
            pairs.setdefault(job['inputs'][0], []).append(job)
        self.assertEqual(len(pairs), 12)
        for pair in pairs.values():
            self.assertEqual(len(pair), 2)
            self.assertEqual(pair[0]['resources'], pair[1]['resources'])
            self.assertNotIn('--adaptive', pair[0]['command'])
            self.assertIn('--adaptive', pair[1]['command'])
            self.assertEqual(pair[0]['scheduled_events'], pair[1]['scheduled_events'])
        largest = [j for j in jobs if 'django-12276' in j['key']]
        self.assertEqual(len(largest), 2)
        for job in largest:
            self.assertEqual((job['scheduled_checkpoints'], job['scheduled_restores'],
                              job['scheduled_events']), (184, 69, 253))

    def test_explicit_small_or_invalid_dedicated_resource_fails_before_plan(self):
        for candidate in (8192, 16383, 0, -1, True, 16384.0, '16384', None):
            with self.subTest(candidate=candidate):
                config = dict(self.config, figure06_adaptive_mem_mib=candidate)
                with self.assertRaisesRegex(ValueError, 'figure-06-adaptive requires'):
                    self.plan(config=config)

    def test_higher_global_default_is_not_silently_lowered(self):
        config, resources = catalog.adaptive_guest_resources(dict(self.config, mem_mib=24576))
        self.assertEqual(config['mem_mib'], 24576)
        self.assertEqual(resources['guest_mem_mib'], 24576)
        self.assertEqual(resources['nominal_non_store_mib'], 10240)

    def test_explicit_experiment_resource_is_independent_and_equal(self):
        jobs = self.plan(config=dict(self.config, figure06_adaptive_mem_mib=20480))
        self.assertEqual(len(jobs), 24)
        self.assertEqual({value(j['command'], '--mem-mib') for j in jobs}, {'20480'})
        self.assertEqual({j['resources']['guest_mem_mib'] for j in jobs}, {20480})

    def test_invalid_global_memory_is_rejected(self):
        for candidate in (True, 0, -1, 8192.0, '8192', None):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, 'mem_mib must be a positive integer'):
                    catalog.adaptive_guest_resources(dict(self.config, mem_mib=candidate))

    def test_other_vm_experiments_stay_at_8192_and_baseline_config_is_unchanged(self):
        config = dict(self.config, figure06_adaptive_mem_mib=20480)
        before = copy.deepcopy(config)
        jobs = self.plan(('table-02-deltabox', 'table-03-slow', 'figure-06-memory',
                          'table-02-fc-diff'), config=config)
        vm_jobs = [j for j in jobs if '--mem-mib' in j['command']]
        self.assertEqual(len(vm_jobs), 28)
        self.assertEqual({value(j['command'], '--mem-mib') for j in vm_jobs}, {'8192'})
        self.assertFalse(any('resources' in j for j in jobs))
        fc_jobs = [j for j in jobs if j['experiment'] == 'table-02-fc-diff']
        self.assertTrue(fc_jobs)
        self.assertTrue(all(value(j['command'], '--config') == str(self.path / 'config.json')
                            for j in fc_jobs))
        self.assertEqual(config, before)

    def test_store_capacity_is_not_reported_as_an_allocation_or_guarantee(self):
        config, resources = catalog.adaptive_guest_resources(self.config)
        self.assertEqual(resources['guest_snapshot_store_cap_mib'], 14336)
        self.assertEqual(resources['nominal_non_store_mib'], 2048)
        self.assertEqual(resources['planning_envelope_mib'],
                         70 * 96 + 114 * 16 + 184 * 20 + 2048)
        self.assertIn('not a strict peak-memory upper bound', resources['limitation'])
        self.assertIn('not preallocated', resources['limitation'])
        self.assertIn('not an enforced or protected reservation', resources['limitation'])
        self.assertIn('unchanged 2 GiB host reserve', resources['host_admission'])

    def test_existing_numa_gate_charges_all_additional_guest_memory(self):
        node = self.path / 'node1'
        node.mkdir()
        (node / 'meminfo').write_text(
            'Node 1 MemFree: 1000000 kB\nNode 1 Active(file): 0 kB\n'
            'Node 1 Inactive(file): 0 kB\nNode 1 Dirty: 0 kB\n'
            'Node 1 Writeback: 0 kB\nNode 1 Mapped: 0 kB\n')
        base, data = self.path / 'base.xfs', self.path / 'data.xfs'
        base.write_bytes(b'base image')
        data.write_bytes(b'data image')
        sources = dict(base_xfs=base, data_xfs=data)
        old = memory_budget(dict(mem_mib=8192), sources, 'policy: bind\nmembind: 1\n',
                            node_root=self.path)
        effective, _ = catalog.adaptive_guest_resources(self.config)
        new = memory_budget(effective, sources, 'policy: bind\nmembind: 1\n',
                            node_root=self.path)
        self.assertEqual(new['required_bytes'] - old['required_bytes'], 8 << 30)
        self.assertEqual(new['reserve_bytes'], 2 << 30)
        self.assertEqual(new['nodes'], [1])
        self.assertLess(new['available_bytes'], new['required_bytes'])

    def test_hosted_example_override_does_not_raise_global_memory(self):
        config = json.loads((ROOT / 'ae/configs/spr4numa-review.json').read_text())
        self.assertEqual(config['mem_mib'], 8192)
        overrides = config['review']['experiment_overrides']
        self.assertEqual(overrides['figure-06-adaptive']['figure06_adaptive_mem_mib'], 16384)
        self.assertNotIn('mem_mib', overrides['figure-06-memory'])
        self.assertNotIn('figure06_adaptive_mem_mib', overrides['table-02-fc-diff'])


if __name__ == '__main__':
    unittest.main()
