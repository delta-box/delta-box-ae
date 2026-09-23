"""Long recorded waits retain their real duration and receive a sufficient deadline."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae'))
from repro import catalog


class CatalogBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trace = self.root / 'paper/input'
        self.trace.mkdir(parents=True)
        data = {'workspace': {'repository': {'commit': 'a' * 40}}, 'transitions': [
            {'id': 0, 'name': 'Pending', 'created_at': '2026-09-22T00:00:00'},
            {'id': 1, 'previous_state_id': 0, 'name': 'EditCode', 'created_at': '2026-09-22T00:00:01'},
            {'id': 2, 'previous_state_id': 1, 'name': 'EditCode', 'created_at': '2026-09-22T08:20:00'},
            {'id': 3, 'previous_state_id': 2, 'name': 'Finished', 'created_at': '2026-09-22T08:20:01'}]}
        (self.trace / 'trajectory.json').write_text(json.dumps(data))

    def test_budget_sums_actual_legacy_schedule_and_keeps_prefix_semantics(self):
        full = catalog.replay_wait_budget(self.trace, 'owner__project-1', legacy=True)
        prefix = catalog.replay_wait_budget(self.trace, 'owner__project-1', legacy=True, max_events=2)
        self.assertEqual(full['recorded_wait_s'], 30000)
        self.assertEqual(prefix['recorded_wait_s'], 29999)
        self.assertIn('not isolated LLM RTT', full['wait_source'])

    def test_long_wait_extends_both_inner_and_outer_deadlines(self):
        config = {'timeout': 100, 'legacy_timing_policy': 'recorded-wall', 'images_dir': '/images', 'kernel': '/kernel', 'base_xfs': '/base'}
        row = {'instance': 'owner__project-1', 'pool': 'pool', 'local': 'paper/input/trajectory.json'}
        with patch.object(catalog, 'AE_ROOT', self.root), patch.object(catalog, 'cohort', return_value=[row]):
            jobs = catalog.build_jobs(['figure-06-adaptive'], config, self.root / 'config.json', self.root / 'out')
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            self.assertEqual(job['recorded_wait_s'], 30000)
            self.assertEqual(job['timeout_s'], 30400)
            command = job['command']
            self.assertEqual(command[command.index('--timeout') + 1], '30100')
            self.assertEqual(job['action_margin_s'], 100)

    def test_prefix_bootstrap_does_not_budget_unplayed_events(self):
        config = {'timeout': 100, 'legacy_timing_policy': 'recorded-wall', 'images_dir': '/images', 'kernel': '/kernel', 'base_xfs': '/base'}
        row = {'instance': 'owner__project-1', 'pool': 'pool', 'local': 'paper/input/trajectory.json'}
        with patch.object(catalog, 'AE_ROOT', self.root), patch.object(catalog, 'cohort', return_value=[row]):
            jobs = catalog.build_jobs(['figure-06-adaptive'], config, self.root / 'config.json', self.root / 'out', max_events=1)
        self.assertEqual(jobs[0]['recorded_wait_s'], 0)
        self.assertEqual(jobs[0]['timeout_s'], 400)
        self.assertEqual(jobs[0]['run_purpose'], 'smoke')

    def test_paper_zero_is_explicit_default_without_erasing_recorded_intervals(self):
        config = {'timeout': 100, 'images_dir': '/images', 'kernel': '/kernel', 'base_xfs': '/base'}
        row = {'instance': 'owner__project-1', 'pool': 'pool', 'local': 'paper/input/trajectory.json'}
        with patch.object(catalog, 'AE_ROOT', self.root), patch.object(catalog, 'cohort', return_value=[row]):
            jobs = catalog.build_jobs(['figure-06-adaptive'], config, self.root / 'config.json', self.root / 'out')
        self.assertEqual(jobs[0]['recorded_wait_s'], 0)
        self.assertEqual(jobs[0]['legacy_timing_policy'], 'paper-zero')
        command = jobs[0]['command']
        self.assertEqual(command[command.index('--legacy-timing-policy') + 1], 'paper-zero')
        from legacy_schedule import convert_legacy
        path = self.root / 'explicit-zero.jsonl'
        metadata = convert_legacy(self.trace, 'owner__project-1', path, timing_policy='paper-zero')
        events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(sum(event.get('latency_ms', 0) for event in events), 0)
        self.assertEqual(sum(event.get('recorded_inter_transition_ms', 0) for event in events), 30000000)
        self.assertEqual(metadata['legacy_timing_policy'], 'paper-zero')
        with self.assertRaisesRegex(ValueError, 'timing policy'):
            convert_legacy(self.trace, 'owner__project-1', path, timing_policy='invented')

    def test_direct_reproduce_run_honors_job_deadline(self):
        spec = importlib.util.spec_from_file_location('reproduce_budget_fixture', ROOT / 'ae/reproduce.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = self.root / 'config.json'
        config.write_text('{"timeout": 1}')
        job = dict(key='fixture', command=['true'], run_purpose='full-cohort', timeout_s=98765)
        with patch.object(sys, 'argv', ['reproduce', 'run', '--config', str(config), '--output', str(self.root / 'out')]), \
             patch.object(module, 'doctor', return_value={'ok': True}), \
             patch.object(module, 'build_jobs', return_value=[job]), \
             patch.object(module, 'repository_state', return_value={}), \
             patch.object(module, 'host_state', return_value={}), \
             patch.object(module, 'from_environment', return_value=None), \
             patch.object(module, 'execute', return_value={'status': 'ok'}) as execute, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(), 0)
        self.assertEqual(execute.call_args.kwargs['timeout'], 98765)


if __name__ == '__main__':
    unittest.main()
