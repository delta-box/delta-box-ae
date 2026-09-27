"""Validate paper-disk evidence without launching a sandbox or modifying timing."""
import argparse
import copy
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae'))
sys.path.insert(0, str(ROOT / 'ae/runners'))
import baseline
import cube_phases
from repro import catalog


def phase(name, start_ms, end_ms, flow='commit_sandbox'):
    return {'snapshotTiming': 'cube_ck_phase', 'flow': flow, 'phase': name,
            'sandboxID': 'sandbox-test', 'templateID': 'snapshot-test',
            'startUnixNs': 1_000_000_000 + start_ms * 1_000_000,
            'endUnixNs': 1_000_000_000 + end_ms * 1_000_000,
            'durationMs': end_ms - start_ms, 'success': True}


def event(kind='ckpt', start_ms=0, end_ms=100, index=0):
    field = 'checkpoint_wall_ms' if kind == 'ckpt' else 'restore_wall_ms'
    api = {'sandbox_id': 'sandbox-test', 'api_start_unix_ns': 1_000_000_000 + start_ms * 1_000_000,
           'api_end_unix_ns': 1_000_000_000 + end_ms * 1_000_000,
           'api_retries': [], field: end_ms - start_ms}
    result = {'kind': kind, 'ok': True, 'ev_i': index, field: end_ms - start_ms,
              'snapshot_id': 'snapshot-test'}
    result['snapshot' if kind == 'ckpt' else 'cube_steps'] = api if kind == 'ckpt' else [api]
    return result


def checkpoint():
    return [phase('rootfs_dump', 10, 20), phase('memory_prepare', 25, 45),
            phase('memory_dump', 50, 80)]


def restore(start_ms=200):
    names = ['resolve_targets', 'resolve_snapshot_objects', 'current_rootfs_resolve',
             'rootfs_derive_new_gen', 'build_restore_config', 'shim_update_restore',
             'delete_old_rootfs', 'persist_rootfs_after_rollback', 'sync_metadata']
    rows = [phase(name, start_ms + 10 + i * 5, start_ms + 14 + i * 5, 'rollback_sandbox')
            for i, name in enumerate(names)]
    rows.append(phase('rollback_total', start_ms + 5, start_ms + 80, 'rollback_sandbox'))
    return rows


def legacy(row):
    return (f"cube_ck_phase_timing flow={row['flow']} phase={row['phase']} "
            f"sandboxID={row['sandboxID']} templateID={row['templateID']} "
            f"start_unix_ns={row['startUnixNs']} end_unix_ns={row['endUnixNs']} "
            f"duration_ms={row['durationMs']}")


class PhaseCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / 'service.log'
        self.log.write_text('preexisting log, excluded\n')
        self.start = cube_phases.begin(self.log)
        self.pilot = self.root / 'pilot.json'
        self.captured = self.root / 'captured.log'

    def collect(self, rows=None, events=None, strict=True):
        if rows is None:
            rows = checkpoint()
        if events is None:
            events = [event()]
        self.log.write_text('preexisting log, excluded\n' + '\n'.join(
            row if isinstance(row, str) else json.dumps(row) for row in rows) + '\n')
        self.pilot.write_text(json.dumps({'ok': True, 'iterations': events}))
        return cube_phases.collect(self.start, self.pilot, self.captured, strict=strict)

    def test_raw_success_and_hashes_preserved_with_repeated_restore_target(self):
        events = [event(), event('restore', 200, 300, 1), event('restore', 400, 500, 2)]
        rows = checkpoint() + restore(200) + restore(400)
        result = self.collect(rows, events)
        self.assertEqual(result['phase_record_count'], 23)
        self.assertEqual([len(e['phase_record_indices']) for e in result['events']], [3, 10, 10])
        self.assertEqual([e['wall_ms'] for e in result['events']], [100, 100, 100])
        self.assertEqual([r['raw_record'] for r in result['raw_phases']], rows)
        self.assertTrue(all(r['success'] is True for r in result['raw_phases']))
        self.assertTrue(all(e['success_evidence'] == 'explicit-json' for e in result['events']))
        self.assertNotIn('preexisting', self.captured.read_text())
        for key, path in [('pilot', self.pilot), ('captured_log', self.captured)]:
            self.assertEqual(result[key]['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_failed_or_missing_success_cannot_be_accepted(self):
        for value in (False, None, 1, 'true'):
            rows = checkpoint()
            if value is None:
                rows[1].pop('success')
            else:
                rows[1]['success'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'success=true'):
                self.collect(rows)

    def test_failed_event_cannot_supply_successful_phase_evidence(self):
        ev = event()
        ev['ok'] = False
        with self.assertRaisesRegex(ValueError, 'Failed Cube event'):
            self.collect(events=[ev])

    def test_legacy_figure1_remains_explicitly_unverified_and_is_forbidden_in_strict_mode(self):
        rows = [legacy(r) for r in checkpoint()]
        result = self.collect(rows, strict=False)
        self.assertFalse(result['strict_success_and_phase_contract'])
        self.assertEqual(result['events'][0]['success_evidence'], 'unverified-legacy')
        self.assertTrue(all(r['success'] is None and r['raw_record'] is None for r in result['raw_phases']))
        with self.assertRaisesRegex(ValueError, 'success=true'):
            self.collect(rows)

    def test_duplicate_required_stage_is_not_silently_deduplicated(self):
        for changed_interval in (False, True):
            rows = checkpoint()
            duplicate = copy.deepcopy(rows[1])
            if changed_interval:
                duplicate['startUnixNs'] += 1_000_000
                duplicate['endUnixNs'] += 1_000_000
            rows.append(duplicate)
            with self.subTest(changed_interval=changed_interval), self.assertRaisesRegex(ValueError, 'Duplicate'):
                self.collect(rows)

    def test_unexpected_stage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            self.collect(checkpoint() + [phase('unrecognized_stage', 85, 90)])

    def test_single_attempt_required(self):
        ev = event()
        ev['snapshot']['api_retries'] = [{'error': 'lost response'}]
        with self.assertRaisesRegex(ValueError, 'single API attempt'):
            self.collect(events=[ev])

    def test_stage_from_wrong_request_window_or_sandbox_is_not_matched(self):
        for field, value in [('sandboxID', 'other-sandbox'), ('templateID', 'other-snapshot'),
                             ('startUnixNs', 999_000_000), ('endUnixNs', 1_101_000_000)]:
            rows = checkpoint()
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'Missing'):
                self.collect(rows)

    def test_inconsistent_api_duration_is_not_reinterpreted_as_control_time(self):
        for nested in (False, True):
            ev = event()
            ev['checkpoint_wall_ms'] = 130
            if nested:
                ev['snapshot']['checkpoint_wall_ms'] = 130
            with self.subTest(nested=nested), self.assertRaisesRegex(ValueError, 'duration'):
                self.collect(events=[ev])

    def test_raw_stage_duration_must_match_its_interval(self):
        rows = checkpoint()
        rows[0]['durationMs'] = 42
        with self.assertRaisesRegex(ValueError, 'phase duration disagrees'):
            self.collect(rows)

    def test_checkpoint_order_and_overlap_are_rejected(self):
        overlap = checkpoint()
        overlap[1]['startUnixNs'] = 1_019_000_000
        overlap[1]['durationMs'] = 26
        wrong_order = checkpoint()
        wrong_order[0]['phase'], wrong_order[1]['phase'] = wrong_order[1]['phase'], wrong_order[0]['phase']
        for rows in (overlap, wrong_order):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, 'overlap|order'):
                self.collect(rows)

    def test_restore_total_cannot_exclude_children_and_children_cannot_overlap(self):
        rows = restore()
        rows[-1]['endUnixNs'] = 1_245_000_000
        rows[-1]['durationMs'] = 40
        with self.assertRaisesRegex(ValueError, 'outside rollback_total'):
            self.collect(rows, [event('restore', 200, 300)])
        rows = restore()
        rows[1]['startUnixNs'] -= 2_000_000
        rows[1]['durationMs'] += 2
        with self.assertRaisesRegex(ValueError, 'overlap'):
            self.collect(rows, [event('restore', 200, 300)])

    def test_raw_integer_timestamps_are_required_without_coercion(self):
        rows = checkpoint()
        rows[0]['startUnixNs'] = str(rows[0]['startUnixNs'])
        with self.assertRaisesRegex(ValueError, 'integer timestamp'):
            self.collect(rows)

    def test_rotated_or_truncated_log_fails(self):
        self.pilot.write_text(json.dumps({'iterations': [event()]}))
        old = self.log.with_suffix('.old')
        self.log.rename(old)
        self.log.write_text('new log\n')
        with self.assertRaisesRegex(ValueError, 'rotated/truncated'):
            cube_phases.collect(self.start, self.pilot, self.captured, strict=True)


class ProfileContractTests(unittest.TestCase):
    def test_identity_is_backend_bound_and_default_phase_identity_unchanged(self):
        self.assertEqual(baseline.resolve_experiment('cube', True), 'figure-01-cube')
        self.assertEqual(baseline.resolve_experiment('cube', True, 'table-02-cube'), 'table-02-cube')
        self.assertEqual(baseline.resolve_experiment('cube'), 'table-02-cube')
        for args in [('cube', True, 'table-02-e2b'), ('e2b', True, 'table-02-e2b'),
                     ('cube', False, 'figure-01-cube'), ('unknown', False, 'table-02-unknown')]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                baseline.resolve_experiment(*args)

    def test_profile_cannot_be_silently_run_on_ram_or_without_phase_evidence(self):
        config = {'cube': {'profile': 'paper-disk'}, 'baseline_storage': 'disk'}
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(baseline.paper_cube_profile(config, 'cube', 'table-02-cube', True))
            for backend, identity, collect, storage in [
                ('e2b', 'table-02-e2b', True, 'disk'), ('cube', 'figure-01-cube', True, 'disk'),
                ('cube', 'table-02-cube', False, 'disk'), ('cube', 'table-02-cube', True, 'tmpfs')]:
                changed = dict(config, baseline_storage=storage)
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    baseline.paper_cube_profile(changed, backend, identity, collect)
        with mock.patch.dict(os.environ, {'AE_MEMORY_JOB': '{}'}), self.assertRaisesRegex(ValueError, 'memory-storage'):
            baseline.paper_cube_profile(config, 'cube', 'table-02-cube', True)

    def test_public_cube_profile_keeps_all_12_inputs_and_651_events(self):
        config = {'cube': {'profile': 'paper-disk'}, 'baseline_storage': 'disk'}
        jobs = catalog.build_jobs(['table-02-cube'], config, Path('/config.json'), Path('/out'))
        self.assertEqual(len(jobs), 12)
        counts = {'ckpt': 0, 'restore': 0}
        for job in jobs:
            command = job['command']
            self.assertIn('--collect-phases', command)
            self.assertEqual(command[command.index('--experiment-id') + 1], 'table-02-cube')
            self.assertNotIn('--limit', command)
            for line in Path(command[command.index('--schedule') + 1]).read_text().splitlines():
                if line.strip():
                    counts[json.loads(line)['type']] += 1
        self.assertEqual(counts, {'ckpt': 317, 'restore': 334})
        self.assertEqual(counts['ckpt'] * 3 + counts['restore'] * 10, 4291)
        ordinary = catalog.build_jobs(['table-02-cube'], {}, Path('/config.json'), Path('/out'))
        self.assertTrue(all('--collect-phases' not in j['command'] for j in ordinary))


class BaselineDiskEvidenceTests(unittest.TestCase):
    """Exercise the real baseline recorder, with only its sandbox/dependencies isolated."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / 'service.log'
        self.log.write_text('')
        self.binary = self.root / 'cubelet'
        self.binary.write_bytes(b'phase instrumentation fixture')
        self.python = self.root / 'venv/bin/python'
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b'test interpreter fixture')
        self.vendor = self.root / 'vendor'
        self.driver = self.vendor / 'finalbench' / baseline.DRIVERS['cube'][0]
        (self.driver / 'scripts').mkdir(parents=True)
        (self.driver / baseline.DRIVERS['cube'][1]).write_text('# isolated fixture\n')
        self.trace = self.root / 'trace.json'
        self.trace.write_text('{}')
        self.schedule = self.root / 'schedule.jsonl'
        self.schedule.write_text('{"type":"ckpt","ckpt_id":0}\n')
        self.config = {'cube': {'profile': 'paper-disk', 'phase_log': str(self.log),
            'phase_binary': str(self.binary), 'sdk': str(self.root), 'api_url': 'http://127.0.0.1:3000',
            'template': 'test', 'proxy_node_ip': '127.0.0.1'}, 'baseline_storage': 'disk',
            'moatless_venv': str(self.python.parent.parent)}
        self.args = argparse.Namespace(backend='cube', collect_phases=True, experiment_id='table-02-cube',
            out=self.root / 'output', config=self.root / 'config.json', trace=self.trace, instance='test__1',
            limit=None, schedule=self.schedule, dry_run=False, timeout=60, repository_commit=None)

    def run_baseline(self, checks):
        verifier = mock.Mock(side_effect=checks)
        def execute(*_args, **_kwargs):
            result = self.args.out / 'driver/results/fixture/pilot_result.json'
            result.parent.mkdir(parents=True)
            result.write_text(json.dumps({'ok': True, 'iterations': [event()]}))
            self.log.write_text('\n'.join(json.dumps(r) for r in checkpoint()) + '\n')
            return {'status': 'ok'}
        runtime = mock.Mock(side_effect=execute)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
            stack.enter_context(mock.patch.dict(sys.modules, {
                'cube_disk': types.SimpleNamespace(verify=verifier),
                'cube_environment': types.SimpleNamespace(capture_cube_environment=mock.Mock(return_value={
                    'template_cpu_millicores': 2000, 'template_memory_mb': 2048, 'template': {}}))}))
            for name, value in [('load_config', self.config), ('host_state', {}), ('repository_state', {}),
                                ('from_environment', {}), ('configure_test_runtime', {}),
                                ('stage_payload', (self.root / 'payload', self.root / 'traces', [])),
                                ('validate_trace_events', None)]:
                stack.enter_context(mock.patch.object(baseline, name, return_value=value))
            stack.enter_context(mock.patch.object(baseline, 'VENDOR', self.vendor))
            stack.enter_context(mock.patch.object(baseline, 'execute', runtime))
            try:
                status = baseline.run(self.args)
            finally:
                self.verifier = verifier
                self.runtime = runtime
        return status

    def test_before_and_after_proof_and_raw_phase_files_are_bound_to_success(self):
        proof = {'storage': 'disk-backed-xfs', 'manifest_sha256': 'a' * 64}
        self.assertEqual(self.run_baseline([proof, proof]), 0)
        record = json.loads((self.args.out / 'run.json').read_text())
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['storage_mode'], 'disk-backed-xfs')
        self.assertEqual(record['experiment'], 'table-02-cube')
        self.assertEqual(self.verifier.call_count, 2)
        self.assertEqual(record['phase_evidence']['raw_phase_records'], 3)
        for filename in ('cube_disk_before.json', 'cube_disk_after.json', 'cubelet-phases.log', 'cube_phases.json'):
            self.assertTrue((self.args.out / filename).is_file())
            matches = [r for r in record['artifacts'] if r['path'] == filename]
            self.assertEqual(len(matches), 1, filename)
            self.assertEqual(matches[0]['sha256'], hashlib.sha256((self.args.out / filename).read_bytes()).hexdigest())

    def test_failed_before_proof_records_failure_without_launching_driver(self):
        with self.assertRaisesRegex(ValueError, 'wrong backing'):
            self.run_baseline([ValueError('wrong backing')])
        self.assertFalse(self.runtime.called)
        record = json.loads((self.args.out / 'run.json').read_text())
        self.assertEqual(record['status'], 'failed')
        self.assertIn('wrong backing', record['error'])

    def test_changed_after_proof_prevents_success_even_when_driver_succeeds(self):
        with self.assertRaisesRegex(ValueError, 'storage changed'):
            self.run_baseline([{'storage': 'disk-backed-xfs'}, ValueError('storage changed')])
        self.assertTrue(self.runtime.called)
        record = json.loads((self.args.out / 'run.json').read_text())
        self.assertEqual(record['status'], 'failed')
        self.assertIn('storage changed', record['error'])


if __name__ == '__main__':
    unittest.main()
