"""Figure 2 audit export must follow worker exit and the last RSS sample."""
from contextlib import ExitStack
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ae.repro.analysis import analyze_fresh
from ae.repro.common import artifact_records, write_json
from ae.repro.replay_audit import summarize

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae/runners'))
spec = importlib.util.spec_from_file_location('profile_audit_runner', ROOT / 'ae/runners/profile.py')
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


def stats(policy='audit', mismatches=1):
    return dict(ok=True, message_policy=policy, cursor=1, total=1, n_served=1,
                n_mismatch=mismatches, n_protocol_errors=0, latency_policy='recorded', sleep_wall_s=0.0,
                audit_records_dropped=0, audit_payloads_omitted=0)


def report(state):
    return dict(ok=True, schema_version=1, message_policy=state['message_policy'],
                latency_policy=state['latency_policy'], stats=dict(state, audit_events_pending=0), records=[], buffer={}, flush_id=1)


class ProfileRunnerAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.output = self.root / 'out'
        self.trace = self.root / 'trajectory.json'
        self.trace.write_text('{}')
        self.config = self.root / 'config.json'
        self.instance = 'sympy__sympy-22840'
        self.source = self.root / 'source'
        (self.source / 'moatless').mkdir(parents=True)
        (self.source / 'moatless/search_tree.py').write_text('# original\n')
        self.ae = self.root / 'ae'
        pinned = self.ae / 'vendor/spr_payload/moatless-det-src/moatless/search_tree.py'
        pinned.parent.mkdir(parents=True)
        pinned.write_text('# profile metrics\n')

    def run_profile(self, *, policy='audit', mismatches=1, timeout=False,
                    stats_interrupt=False, audit_failure=False):
        events = []
        state = stats(policy, mismatches)
        self.config.write_text(json.dumps({'moatless_venv': str(self.root / 'venv'),
                                          'replay_message_policy': policy}))
        owner = self

        class Process:
            def __init__(self, kind):
                self.kind = kind
                self.pid = 701 if kind == 'mock' else 702
                self.returncode = None
                self.polls = 0
            def poll(self):
                self.polls += 1
                if self.kind == 'worker' and self.polls > 1:
                    self.returncode = 0
                return self.returncode
            def wait(self, *, timeout):
                owner.assertEqual(timeout, 5)
                events.append('wait_' + self.kind)
                self.returncode = 0
                return 0

        def stage(config, trace, instance, output, **kwargs):
            self.assertTrue(kwargs['allow_missing_rtt'])
            payload = output / 'payload'
            payload.mkdir()
            (payload / 'moatless-det-src').symlink_to(self.source)
            (payload / 'repos' / ('swe-bench_' + instance)).mkdir(parents=True)
            traces = output / 'mock_traces'
            traces.mkdir()
            return payload, traces, []

        def dependencies(config, output, env):
            events.append('offline_dependencies')
            env['NLTK_DATA'] = str(output / 'nltk_data')
            env['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
            return {'nltk_data': {'fixture': True}}

        def popen(command, **kwargs):
            self.assertEqual(kwargs['env']['MOCK_MESSAGE_POLICY'], policy)
            self.assertEqual(kwargs['env']['MOCK_LATENCY_POLICY'], 'recorded')
            self.assertEqual(kwargs['env']['LITELLM_LOCAL_MODEL_COST_MAP'], 'True')
            self.assertIn('offline_dependencies', events)
            kind = 'mock' if 'mock_llm_server.py' in command[1] else 'worker'
            events.append('start_' + kind)
            if kind == 'worker':
                self.assertIn('--defer-audit', command)
                self.assertIn('--skip-mock-spawn', command)
                (self.output / 'step_metrics.jsonl').write_text(json.dumps(
                    dict(soft_dirty_bytes=4096, action_write_bytes=12)) + '\n')
            return Process(kind)

        def http(port, path):
            if path == 'healthz':
                return {'ok': True}
            self.assertIn('wait_worker', events)
            self.assertTrue((self.output / 'tree_rss_samples.json').exists())
            events.append('stats')
            if stats_interrupt:
                raise KeyboardInterrupt('SIGTERM during stats')
            return state

        def flush(url, path, *, primary_error):
            self.assertEqual(os.environ['MOCK_MESSAGE_POLICY'], policy)
            self.assertIn('wait_worker', events)
            self.assertEqual(events.count('sample'), 1)
            events.append('flush')
            if timeout or stats_interrupt:
                self.assertIsInstance(primary_error, TimeoutError if timeout else KeyboardInterrupt)
            else:
                self.assertIsNone(primary_error)
            exported = report(state)
            if audit_failure:
                exported['audit_error'] = {'type': 'TimeoutError', 'message': 'audit unavailable'}
            write_json(path, exported)
            return exported

        def sample(pid):
            events.append('sample')
            return [(pid, 1024)]

        argv = ['profile.py', '--config', str(self.config), '--instance', self.instance,
                '--trace', str(self.trace), '--panel', 'memory', '--out', str(self.output), '--timeout', '1']
        with ExitStack() as stack:
            for name, value in (('stage_payload', stage), ('stage_local_dependencies', dependencies),
                                ('http', http), ('flush_audit', flush), ('sample', sample)):
                stack.enter_context(patch.object(profile, name, side_effect=value))
            stack.enter_context(patch.object(profile, 'AE_ROOT', self.ae))
            stack.enter_context(patch.object(profile, 'host_state', return_value={}))
            stack.enter_context(patch.object(profile, 'repository_state', return_value={}))
            stack.enter_context(patch.object(profile.subprocess, 'Popen', side_effect=popen))
            stack.enter_context(patch.object(profile.subprocess, 'check_output', side_effect=['a' * 40, '12 repo']))
            stack.enter_context(patch.object(profile.os, 'killpg', side_effect=lambda pid, sig: events.append('kill_' + str(pid))))
            stack.enter_context(patch.object(profile.time, 'sleep'))
            stack.enter_context(patch.object(profile.time, 'monotonic', side_effect=[0, 0, 2 if timeout else 0]))
            stack.enter_context(patch.object(sys, 'argv', argv))
            stack.enter_context(patch.dict(os.environ, {'MOCK_MESSAGE_POLICY': 'parent-unchanged', 'MOCK_LATENCY_POLICY': 'zero'}))
            caught = None
            try:
                profile.main()
            except BaseException as error:
                caught = error
            self.assertEqual(os.environ['MOCK_MESSAGE_POLICY'], 'parent-unchanged')
        manifest = json.loads((self.output / 'run.json').read_text())
        self.assertLess(events.index('wait_worker'), events.index('flush'))
        self.assertLess(events.index('flush'), events.index('wait_mock'))
        self.assertTrue((self.output / 'tree_rss_samples.json').exists())
        self.assertTrue((self.output / 'mock_audit.json').exists())
        self.assertIn('mock_audit.json', [row['path'] for row in manifest['artifacts']])
        return manifest, caught

    def test_audit_mismatch_survives_and_export_follows_rss_window(self):
        manifest, caught = self.run_profile()
        self.assertIsNone(caught)
        self.assertEqual(manifest['status'], 'ok')
        self.assertEqual(manifest['replay_audit']['message_equivalence'], 'different')

    def test_strict_policy_reaches_parent_client_and_rejects_mismatch(self):
        manifest, caught = self.run_profile(policy='strict')
        self.assertIsInstance(caught, ValueError)
        self.assertEqual(manifest['status'], 'failed')

    def test_timeout_preserves_primary_error_samples_and_audit_failure(self):
        manifest, caught = self.run_profile(timeout=True, audit_failure=True)
        self.assertIsInstance(caught, TimeoutError)
        self.assertEqual(manifest['status'], 'failed')
        self.assertIn('Profile timeout', manifest['error'])

    def test_first_sigterm_during_final_stats_still_flushes_reaps_and_records_failure(self):
        manifest, caught = self.run_profile(stats_interrupt=True)
        self.assertIsInstance(caught, KeyboardInterrupt)
        self.assertEqual(manifest['status'], 'failed')
        self.assertIn('SIGTERM during stats', manifest['error'])


class ProfileAnalysisAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def fixture(self, name='run', *, policy='audit', mismatches=1):
        directory = self.root / name
        directory.mkdir()
        state = stats(policy, mismatches)
        exported = report(state)
        (directory / 'step_metrics.jsonl').write_text(json.dumps(
            dict(soft_dirty_bytes=4096, action_write_bytes=12)) + '\n')
        write_json(directory / 'tree_rss_samples.json', [{'rss_kb_total': 1024}])
        write_json(directory / 'mock_stats.json', state)
        write_json(directory / 'mock_audit.json', exported)
        config = dict(experiment='figure-02-memory', instance='sympy__sympy-22840',
                      analysis_mode='fresh-measurement', status='ok', step_count=1, rss_sample_count=1,
                      message_policy=policy, replay_audit=summarize([exported], policy))
        self.save(directory, config)
        return directory, config

    def save(self, directory, config):
        config['artifacts'] = artifact_records(directory, [directory / name for name in
            ('step_metrics.jsonl', 'tree_rss_samples.json', 'mock_stats.json', 'mock_audit.json')])
        write_json(directory / 'run.json', config)

    def test_audit_differences_are_retained_with_explicit_equivalence_label(self):
        self.fixture()
        result = analyze_fresh(self.root)['experiments']['figure-02']
        self.assertEqual(result['replay_audits'][0]['message_equivalence'], 'different')
        self.assertTrue(any('message differences' in row for row in result['limitations']))

    def test_sidecar_hash_and_summary_are_both_verified(self):
        directory, config = self.fixture()
        audit = directory / 'mock_audit.json'
        audit.write_text(audit.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            analyze_fresh(self.root)
        self.save(directory, config)
        config['replay_audit']['n_mismatch'] = 0
        write_json(directory / 'run.json', config)
        with self.assertRaisesRegex(ValueError, 'audit summary'):
            analyze_fresh(self.root)

    def test_cursor_protocol_strict_and_stats_disagreement_remain_fatal(self):
        directory, config = self.fixture(mismatches=0)
        original = deepcopy(config)
        cases = [({'cursor': 0}, 'incomplete'), ({'total': 0, 'cursor': 0}, 'incomplete'),
                 ({'n_protocol_errors': 1}, 'protocol'),
                 ({'message_policy': 'strict', 'n_mismatch': 1}, 'mismatch'),
                 ({'n_mismatch': 2}, 'stats differ')]
        for change, error in cases:
            with self.subTest(change=change):
                modified = dict(stats(mismatches=0), **change)
                write_json(directory / 'mock_stats.json', modified)
                current = deepcopy(original)
                if change.get('message_policy'):
                    current['message_policy'] = change['message_policy']
                self.save(directory, current)
                with self.assertRaisesRegex(ValueError, error):
                    analyze_fresh(self.root)

    def test_new_policy_requires_manifest_bound_audit_sidecar(self):
        directory, config = self.fixture()
        config['artifacts'] = [row for row in config['artifacts'] if row['path'] != 'mock_audit.json']
        write_json(directory / 'run.json', config)
        with self.assertRaisesRegex(ValueError, 'not bound'):
            analyze_fresh(self.root)


if __name__ == '__main__':
    unittest.main()
