"""Fixed baseline selection, complete traces, and hosted argument forwarding."""
import csv
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae'))
from repro import catalog

spec = importlib.util.spec_from_file_location('hosted_44', ROOT / 'ae/scripts/hosted_launcher.py')
hosted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosted)


class Baseline44Tests(unittest.TestCase):
    def rows(self, backend):
        suffix = 'criu-attempts' if backend == 'criu' else backend
        with (ROOT / f'ae/paper/table-02/cohort-{suffix}.csv').open() as f:
            return list(csv.DictReader(f))

    def test_three_backends_use_identical_44_ids_and_complete_trajectories(self):
        ids = []
        for backend in ('replay', 'criu', 'fc-diff'):
            with self.subTest(backend=backend), patch.object(catalog, 'cohort', return_value=self.rows(backend)):
                jobs = catalog.build_jobs([f'table-02-{backend}'], {'baseline_inputs': '44'}, ROOT / 'config.json', Path('/results'))
            self.assertEqual(len(jobs), 44)
            self.assertTrue(all(j['run_purpose'] == 'full-trace' for j in jobs))
            self.assertTrue(all('--limit' not in j['command'] for j in jobs))
            self.assertTrue(all(j['input_selection']['instances'] == 44 for j in jobs))
            ids.append([j['key'].removeprefix(f'table-02-{backend}__') for j in jobs])
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[1], ids[2])

    def test_all_keeps_original_sets(self):
        for backend, count in [('replay', 244), ('criu', 244), ('fc-diff', 238)]:
            with self.subTest(backend=backend), patch.object(catalog, 'cohort', return_value=self.rows(backend)):
                jobs = catalog.build_jobs([f'table-02-{backend}'], {'baseline_inputs': 'all'}, ROOT / 'config.json', Path('/results'))
                self.assertEqual(len(jobs), count)
                self.assertTrue(all(j['run_purpose'] == 'full-cohort' for j in jobs))

    def test_missing_or_replaced_selected_input_is_an_error(self):
        selected = json.loads((ROOT / 'ae/paper/table-02/cohort-44.json').read_text())['instances']
        rows = self.rows('criu')
        with self.assertRaisesRegex(ValueError, 'missing'):
            catalog.baseline_rows('criu', [r for r in rows if r['instance'] != selected[0]], {'baseline_inputs': '44'})
        for row in rows:
            if row['instance'] == selected[0]:
                row['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'binding differs'):
            catalog.baseline_rows('criu', rows, {'baseline_inputs': '44'})

    def test_duplicate_manifest_does_not_silently_reduce_count(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(catalog, 'AE_ROOT', Path(tmp)):
            p = Path(tmp) / 'paper/table-02/cohort-44.json'
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({'schema_version': 1, 'instances': ['duplicate'] * 44}))
            with self.assertRaisesRegex(ValueError, '44 unique'):
                catalog.baseline_rows('criu', [], {'baseline_inputs': '44'})

    def test_nonselected_backends_keep_all_their_inputs(self):
        for backend in ('cube', 'e2b'):
            rows = self.rows(backend)
            chosen, selection = catalog.baseline_rows(backend, rows, {'baseline_inputs': '44'})
            self.assertEqual(chosen, rows)
            self.assertIsNone(selection)

    def test_limit_is_applied_after_fixed_selection(self):
        with patch.object(catalog, 'cohort', return_value=self.rows('criu')):
            jobs = catalog.build_jobs(['table-02-criu'], {'baseline_inputs': '44'}, ROOT / 'config.json', Path('/results'), limit=1)
        expected = json.loads((ROOT / 'ae/paper/table-02/cohort-44.json').read_text())['instances'][0]
        self.assertEqual([j['key'] for j in jobs], ['table-02-criu__' + expected])
        self.assertEqual(jobs[0]['run_purpose'], 'quick-check')

    def test_hosted_launcher_forwards_both_choices(self):
        policy = {'python': Path('/usr/bin/python3'), 'runtime_root': ROOT, 'config': Path('/etc/review.json')}
        for value in ('44', 'all'):
            args = hosted.parse_arguments(['--checkout', str(ROOT), '--baseline-inputs', value])
            command = hosted.command_line(policy, args, Path('/results'))
            self.assertEqual(command[command.index('--baseline-inputs') + 1], value)

if __name__ == '__main__':
    unittest.main()
