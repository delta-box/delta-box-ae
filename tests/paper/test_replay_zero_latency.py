"""Replay recorded-RTT paper accounting and optional direct zero-latency timing."""
from contextlib import ExitStack
import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.paper.test_mock_message_policy import server, completion
from tests.paper.test_baseline_audit_drivers import REPLAY
from ae.runners import baseline
from ae.repro.analysis import analyze_fresh, fresh_labels
from ae.repro.replay_audit import summarize


class ZeroLatencyTests(unittest.TestCase):
    def request(self, state, messages):
        handler = server.MockHandler.__new__(server.MockHandler)
        handler.server = SimpleNamespace(state=state)
        handler._read_body = lambda: json.dumps({'messages': messages}).encode()
        handler._send_json = Mock()
        handler._handle_chat_completions()
        return handler

    def test_zero_policy_keeps_recorded_response_cursor_and_duration_without_sleep(self):
        messages = [{'role': 'user', 'content': 'same recording'}]
        state = server.ServerState(Path('/unused'), latency_policy='zero')
        item = completion(messages); item.dur_s = 300.0
        state.sequence = [item]
        with patch.object(server.time, 'sleep', side_effect=AssertionError('injected sleep')):
            handler = self.request(state, messages)
        handler._send_json.assert_called_once_with(200, item.response)
        self.assertEqual(item.dur_s, 300.0)
        self.assertEqual((state.cursor, state.n_served), (1, 1))
        report = state.flush_audit()
        self.assertEqual(report['latency_policy'], 'zero')
        self.assertEqual(report['stats']['latency_policy'], 'zero')
        self.assertEqual(report['stats']['sleep_wall_s'], 0.0)
        state.rewind(0)
        with patch.object(server.time, 'sleep', side_effect=AssertionError('injected sleep')):
            self.request(state, messages)
        self.assertEqual(state.n_served, 2)

    def test_recorded_is_default_and_preserves_compensated_sleep(self):
        with patch.dict(os.environ, {}, clear=True):
            state = server.ServerState(Path('/unused'))
        self.assertEqual(state.latency_policy, 'recorded')
        messages = [{'role': 'user', 'content': 'same recording'}]
        item = completion(messages); item.dur_s = 12.5
        state.sequence = [item]
        with patch.object(server.time, 'perf_counter', side_effect=[100., 100.5, 100.5, 112.5]), \
                patch.object(server.time, 'sleep') as sleep:
            self.request(state, messages)
        sleep.assert_called_once_with(12.)
        self.assertEqual(state.stats()['sleep_wall_s'], 12.)

    def test_policy_is_validated_and_baselines_override_ambient_setting(self):
        with self.assertRaises(ValueError):
            server.ServerState(Path('/unused'), latency_policy='fast')
        with patch.dict(os.environ, {'MOCK_LATENCY_POLICY': 'zero'}):
            self.assertEqual(server.ServerState(Path('/unused')).latency_policy, 'zero')
            self.assertEqual(server.ServerState(Path('/unused'), latency_policy='recorded').latency_policy, 'recorded')
        for backend in ('replay', 'criu', 'fc-diff', 'profile'):
            env = {'MOCK_LATENCY_POLICY': 'zero' if backend != 'replay' else 'recorded'}
            identity = baseline.configure_mock_latency(backend, env)
            expected = 'zero' if backend == 'replay' else 'recorded'
            self.assertEqual(env['MOCK_LATENCY_POLICY'], expected)
            self.assertEqual(identity['mock_latency_policy'], expected)

    def test_replay_driver_passes_explicit_zero_even_with_recorded_environment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MOCK_LATENCY_POLICY': 'recorded'}), \
                patch.object(REPLAY.subprocess, 'Popen') as popen, patch.object(REPLAY, 'wait_healthz'):
            process = REPLAY.start_mock(1234, Path(tmp) / 'mock.log', Path(tmp) / 'audit.json')
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index('--latency-policy') + 1], 'zero')
            process._finalbench_logf.close()

    def test_paper_accounting_uses_served_recorded_prefix_not_sleep_wall(self):
        sequence = [SimpleNamespace(dur_s=12.5), SimpleNamespace(dur_s=4.0),
                    SimpleNamespace(dur_s=99.0)]
        loader = SimpleNamespace(load_trajectory=Mock(return_value=sequence))
        with patch.dict("sys.modules", {"trajectory_index": loader}), \
                patch.object(REPLAY, "MOCK_LATENCY_POLICY", "recorded"):
            stats = {"cursor": 2, "n_served": 2, "sleep_wall_s": 16.4}
            self.assertEqual(REPLAY.completion_wait_ms("case", stats, 3), 16500.)
            for cursor, served in [(4, 4), (-1, -1), (True, 1), (2, 3), (None, 0)]:
                with self.subTest(cursor=cursor, served=served), self.assertRaises(ValueError):
                    REPLAY.completion_wait_ms("case", {"cursor": cursor, "n_served": served}, 3)
            self.assertEqual(REPLAY.completion_wait_ms("case", {}, 0), 0.)
        with patch.object(REPLAY, "MOCK_LATENCY_POLICY", "zero"):
            self.assertEqual(REPLAY.completion_wait_ms("case", {}, 3), 0.)

    def test_audit_cannot_claim_zero_with_sleep_or_wrong_policy(self):
        report = {'latency_policy': 'zero', 'stats': {'latency_policy': 'zero', 'sleep_wall_s': 0.0}}
        baseline.validate_mock_latency([report], 'zero')
        for patch_stats in ({'sleep_wall_s': 1}, {'latency_policy': 'recorded'}, {'sleep_wall_s': None}):
            with self.subTest(stats=patch_stats), self.assertRaises(ValueError):
                baseline.validate_mock_latency([dict(report, stats={**report['stats'], **patch_stats})], 'zero')


class ReplayAnalysisIdentityTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)

    def sample(self, name, zero, *, override=None):
        root = self.root / name; (root/'results').mkdir(parents=True)
        identity = {'mock_latency_policy': 'zero', 'replay_timing_method': 'zero-latency-wall'} if zero else {}
        instance = 'sympy__sympy-22840'
        stats = dict(message_policy='audit', n_mismatch=0, n_protocol_errors=0,
                     **({'latency_policy':'zero', 'sleep_wall_s':0.0} if zero else {}))
        report = dict(ok=True, schema_version=1, message_policy='audit', stats=stats,
                      **({'latency_policy':'zero'} if zero else {}))
        summary = dict(instance=instance, ok=True, requested_restores=1, completed_restores=1, **identity)
        row = dict(instance=instance, ok='True', rc='0', mock_mismatch=0, mock_protocol_errors=0,
                   message_policy='audit', target_expansions=3, restore_index=0, copytree_ms=2,
                   restore_ms=20, replay_ms=18, mock_sleep_ms=0 if zero else 10,
                   restore_zero_llm_ms=20 if zero else 10, rmtree_ms=0,
                   replay_zero_llm_ms=18 if zero else 8, **identity)
        config = dict(analysis_mode='fresh-measurement', status='ok', experiment='table-02-replay',
                      backend='replay', instance=instance, counts=dict(checkpoints=1, restores=1),
                      message_policy='audit', replay_audit=summarize([report], 'audit'), **identity)
        if override: override(config, summary, row, report)
        artifacts = []
        for name, obj in [('summary.json',summary), ('restore_000.mock_audit.json',report), ('restores.csv', row)]:
            p=root/'results'/name
            if name.endswith('.csv'):
                with p.open('w') as stream:
                    writer=csv.DictWriter(stream, fieldnames=list(obj));writer.writeheader();writer.writerow(obj)
            else: p.write_text(json.dumps(obj))
            raw=p.read_bytes();artifacts.append(dict(path='results/'+name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        config['artifacts']=artifacts;config['result']=artifacts[0]
        (root/'run.json').write_text(json.dumps(config))
        return root

    def test_old_subtracted_estimates_and_new_zero_wall_are_separate(self):
        self.sample('old',False);self.sample('new',True)
        result=analyze_fresh(self.root)['experiments']
        rows=result['table-02']['metrics']
        self.assertEqual({r['backend'] for r in rows},{'replay-sleep-subtracted-estimate','replay-zero-llm'})
        self.assertEqual(len({r['cohort'] for r in rows}),2)
        self.assertEqual(len({r['plot_group'] for r in rows}),2)
        self.assertEqual(len({r['cohort'] for r in result['figure-01']['metrics']}),2)
        self.assertIn('estimates', ' '.join(result['figure-01']['limitations']))

    def test_recorded_paper_policy_uses_rtt_operand_and_keeps_actual_sleep_separate(self):
        def paper(config, summary, row, report):
            identity = dict(mock_latency_policy='recorded', replay_timing_method='recorded-sleep-subtracted')
            for item in (config, summary, row):
                item.update(identity)
            row.update(mock_completion_wait_ms=10, mock_sleep_wall_ms=9,
                       llm_accounting='served-completion-recorded-rtt-prefix')
            report.update(latency_policy='recorded')
            report['stats'].update(latency_policy='recorded', sleep_wall_s=.009)
        self.sample('paper', False, override=paper)
        metrics = analyze_fresh(self.root)['experiments']['table-02']['metrics']
        rows = [r for r in metrics if r['metric']=='restore_ms' and r['group']=='All']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['backend'], 'replay-sleep-subtracted-estimate')
        self.assertEqual(rows[0]['value'], 10.)

    def test_each_new_identity_layer_and_raw_sleep_is_validated(self):
        edits = [lambda c,s,r,a:r.update(mock_latency_policy='recorded'),
                 lambda c,s,r,a:s.update(replay_timing_method='subtracted'),
                 lambda c,s,r,a:r.update(mock_sleep_ms=10,restore_zero_llm_ms=10,replay_zero_llm_ms=8),
                 lambda c,s,r,a:a['stats'].update(sleep_wall_s=1),
                 lambda c,s,r,a:c.pop('mock_latency_policy'),
                 lambda c,s,r,a:(c.pop('mock_latency_policy'),c.pop('replay_timing_method'))]
        for i,edit in enumerate(edits):
            with self.subTest(edit=i):
                import shutil
                for path in self.root.iterdir():shutil.rmtree(path)
                self.sample('new',True,override=edit)
                with self.assertRaises(ValueError):analyze_fresh(self.root)

    def test_legacy_wall_and_paper_zero_never_share_population(self):
        legacy=dict(experiment='figure-06-adaptive',conversion_policy='legacy-recorded-diff-and-file-context')
        old=fresh_labels(legacy)
        new=fresh_labels(dict(legacy,legacy_timing_policy='paper-zero'))
        self.assertEqual(old['legacy_timing_policy'],'recorded-wall')
        self.assertNotEqual(old['cohort'],new['cohort'])
        self.assertNotEqual(old['plot_group'],new['plot_group'])
        self.assertIsNone(fresh_labels({'experiment':'table-02-deltabox'})['legacy_timing_policy'])


class SourceIdentityTests(unittest.TestCase):
    def test_release_hash_takes_priority_and_all_recorded_locations_are_supported(self):
        from ae.repro.analysis import source_identity
        release = {'source_sha256': 'a'*64}
        for config in ({'release': release}, {'runtime_fingerprint': {'release': release}},
                       {'source_provenance': {'release': release}}):
            self.assertEqual(source_identity(config), 'release-sha256:'+'a'*64)
        with self.assertRaises(ValueError):
            source_identity({'release':release,'source_provenance':{'release':{'source_sha256':'b'*64}}})

    def test_file_identity_ignores_machine_paths_but_changes_with_source_bytes(self):
        from ae.repro.analysis import source_identity
        a = {'runtime_fingerprint': {'files': {'guest.py': {'sha256':'a'*64,'path':'/host/a'}}}}
        b = {'runtime_fingerprint': {'files': {'guest.py': {'sha256':'a'*64,'path':'/other/b'}}}}
        self.assertEqual(source_identity(a),source_identity(b))
        b['runtime_fingerprint']['files']['guest.py']['sha256']='b'*64
        self.assertNotEqual(source_identity(a),source_identity(b))
        self.assertEqual(source_identity({'runtime':{'path':'/checkout'}}),'unknown')
        self.assertNotEqual(fresh_labels({})['cohort'],fresh_labels(a)['cohort'])

    def test_analyzing_two_release_versions_keeps_both_statistical_populations(self):
        from tests.paper.test_analysis import delta, write
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name, source in (('old','a'),('new','b')):
                directory,config=delta(root,name)
                config['release']={'source_sha256':source*64}
                write(directory/'run.json',config)
            rows=analyze_fresh(root)['experiments']['table-02']['metrics']
            self.assertEqual(len({r['source_identity'] for r in rows}),2)
            self.assertEqual(len({r['cohort'] for r in rows}),2)
            self.assertEqual(len({r['plot_group'] for r in rows}),2)

    def test_fanout_isolates_versions_but_compares_backends_of_the_same_source(self):
        from tests.paper.test_analysis import fanout, write
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,backend,source in (('old','deltabox','a'),('new','deltabox','b'),('cube','cube','b')):
                directory,config=fanout(root,backend,name=name)
                config['release']={'source_sha256':source*64}
                write(directory/'run.json',config)
            rows=analyze_fresh(root)['experiments']['figure-08']['series']
            self.assertEqual(len({r['cohort'] for r in rows}),3)
            self.assertEqual(len({r['plot_group'] for r in rows}),2)


if __name__ == '__main__':
    unittest.main()
