"""External sandbox failures must retain evidence and release known resources."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
path = ROOT / 'ae/vendor/finalbench/official_sandbox_fork/bench_official_fork.py'
spec = importlib.util.spec_from_file_location('ae_official_fork_test', path)
bench = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bench
spec.loader.exec_module(bench)


class CleanupTests(unittest.TestCase):
    def run_cube_failure(self, failure, forks):
        created = []
        class Source:
            killed = False
            def clone(self, **kw): raise failure
            def kill(self): self.killed = True
        class Sandbox:
            @staticmethod
            def create(**kw):
                source = Source(); created.append(source); return source
        sdk = types.SimpleNamespace(Config=lambda **kw: kw, Sandbox=Sandbox)
        args = types.SimpleNamespace(cube_api_url='http://unused', cube_template='fixture',
                                     cube_proxy_node_ip='', timeout=10, request_timeout=2,
                                     exec_timeout=2, mem_mib=1, max_workers=1)
        with patch.dict(sys.modules, {'cubesandbox': sdk}), patch.object(bench, 'cube_write_state'), contextlib.redirect_stdout(io.StringIO()):
            try:
                result = bench.bench_cube(args, forks)
            finally:
                self.assertEqual(len(created), 1 if isinstance(failure, KeyboardInterrupt) else len(forks))
                self.assertTrue(all(s.killed for s in created))
        return result

    def test_cube_waits_for_capacity_only_before_source_creation(self):
        from unittest.mock import Mock
        create = Mock(side_effect=[RuntimeError('code 130597: no more resource'), 'source'])
        with patch.object(bench.time, 'sleep') as sleep:
            self.assertEqual(bench.cube_create_after_cleanup(create), ('source', 2))
        sleep.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, 'unrelated'):
            bench.cube_create_after_cleanup(Mock(side_effect=RuntimeError('unrelated')))
        with self.assertRaisesRegex(RuntimeError, '130597'):
            bench.cube_create_after_cleanup(Mock(side_effect=RuntimeError('130597')), timeout_s=0)

    def test_clone_failure_preserves_error_and_cleans_source(self):
        result = self.run_cube_failure(RuntimeError('injected clone failure'), [1])
        self.assertFalse(result[0]['success'])
        self.assertIn('injected clone failure', result[0]['error'])

    def test_cancellation_cleans_source_and_does_not_launch_later_arms(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_cube_failure(KeyboardInterrupt('cancelled'), [1, 4])


class E2BCleanupTests(unittest.TestCase):
    def run_e2b(self, forks=1, batch_size=16, *, kill_outcomes=None,
                snapshot_delete=True, default_kill=True):
        state = {'created': [], 'kills': [], 'snapshots': [], 'deletes': []}
        outcomes = {key: list(values) for key, values in (kill_outcomes or {}).items()}
        clock = [0.0]

        def result(value):
            if isinstance(value, BaseException):
                raise value
            return value

        class Instance:
            def __init__(self, sandbox_id):
                self.sandbox_id = sandbox_id

            def create_snapshot(self, **kw):
                state['snapshots'].append(kw)
                return types.SimpleNamespace(snapshot_id='snapshot-id:default')

            def kill(self, **kw):
                state['kills'].append(self.sandbox_id)
                clock[0] += 100
                pending = outcomes.get(self.sandbox_id, [])
                return result(pending.pop(0) if pending else default_kill)

        class Sandbox:
            @staticmethod
            def create(**kw):
                metadata = kw['metadata']
                sandbox_id = 'source' if metadata['role'] == 'source' else 'child-' + metadata['index']
                state['created'].append(sandbox_id)
                return Instance(sandbox_id)

            @staticmethod
            def delete_snapshot(snapshot_id, **kw):
                state['deletes'].append(snapshot_id)
                clock[0] += 100
                return result(snapshot_delete)

        args = types.SimpleNamespace(e2b_api_url='http://unused', e2b_sandbox_url='http://unused-proxy',
                                     e2b_api_key='', e2b_template='fixture', timeout=10, exec_timeout=2,
                                     mem_mib=1, max_workers=16, e2b_batch_size=batch_size)
        with patch.dict(sys.modules, {'e2b': types.SimpleNamespace(Sandbox=Sandbox)}), \
             patch.object(bench, 'e2b_write_state'), patch.object(bench, 'e2b_run_shell'), \
             patch.object(bench, 'now_ms', side_effect=lambda: clock[0]), \
             contextlib.redirect_stdout(io.StringIO()):
            rows = bench.bench_e2b(args, [forks])
        return rows[0], state

    def test_unnamed_snapshot_success_releases_all_resources(self):
        row, state = self.run_e2b()
        self.assertTrue(row['success'])
        self.assertEqual(row['success_count'], 1)
        self.assertEqual(len(state['snapshots']), 1)
        self.assertNotIn('name', state['snapshots'][0])
        self.assertEqual(state['deletes'], ['snapshot-id:default'])
        self.assertCountEqual(state['kills'], ['child-0', 'source'])
        self.assertEqual([entry['status'] for entry in row['cleanup']], ['ok'] * 3)
        self.assertTrue(all(entry['returned'] is True for entry in row['cleanup']))

    def test_legacy_none_cleanup_returns_remain_successful(self):
        row, state = self.run_e2b(default_kill=None, snapshot_delete=None)
        self.assertTrue(row['success'])
        self.assertCountEqual(state['kills'], ['child-0', 'source'])
        self.assertTrue(all(entry['returned'] is None for entry in row['cleanup']))

    def test_false_snapshot_delete_fails_row_and_still_cleans_source(self):
        row, state = self.run_e2b(snapshot_delete=False)
        self.assertFalse(row['success'])
        self.assertEqual(row['success_count'], 1)
        self.assertCountEqual(state['kills'], ['child-0', 'source'])
        failure = next(entry for entry in row['cleanup'] if entry['resource'] == 'snapshot')
        self.assertEqual(failure['status'], 'failed')
        self.assertIs(failure['returned'], False)
        self.assertIn('returned False', failure['error'])

    def test_false_child_kill_fails_row_and_still_cleans_snapshot_and_source(self):
        row, state = self.run_e2b(kill_outcomes={'child-0': [False]})
        self.assertFalse(row['success'])
        self.assertEqual(state['deletes'], ['snapshot-id:default'])
        self.assertCountEqual(state['kills'], ['child-0', 'source'])
        self.assertEqual(row['cleanup'][0]['status'], 'failed')

    def test_cleanup_exceptions_do_not_skip_other_owned_resources(self):
        row, state = self.run_e2b(2, kill_outcomes={'child-0': [RuntimeError('child unavailable')]},
                                  snapshot_delete=RuntimeError('snapshot unavailable'))
        self.assertFalse(row['success'])
        self.assertEqual(row['success_count'], 2)
        self.assertCountEqual(state['kills'], ['child-0', 'child-1', 'source'])
        self.assertEqual(state['deletes'], ['snapshot-id:default'])
        errors = [entry['error'] for entry in row['cleanup'] if entry['status'] == 'failed']
        self.assertEqual(errors, ['RuntimeError: child unavailable', 'RuntimeError: snapshot unavailable'])

    def test_inter_batch_failure_retains_child_for_final_retry(self):
        for failure in (False, RuntimeError('inter-batch deletion unavailable')):
            with self.subTest(failure=failure):
                row, state = self.run_e2b(2, batch_size=1, kill_outcomes={'child-0': [failure, True]})
                self.assertFalse(row['success'])
                self.assertIn('inter-batch child cleanup failed', row['error'])
                self.assertEqual(state['created'], ['source', 'child-0'])
                self.assertEqual(state['kills'], ['child-0', 'child-0', 'source'])
                self.assertEqual(state['deletes'], ['snapshot-id:default'])
                child_records = [entry for entry in row['cleanup'] if entry['resource'] == 'child']
                self.assertEqual([(entry['phase'], entry['status']) for entry in child_records],
                                 [('inter-batch', 'failed'), ('final', 'ok')])

    def test_final_cleanup_is_outside_ready_latency(self):
        # Only cleanup advances the fixture clock: one inter-batch deletion is
        # measured; final child, snapshot and source deletion are not.
        row, _ = self.run_e2b(2, batch_size=1)
        self.assertTrue(row['success'])
        self.assertEqual(row['ready_e2e_ms'], 100)
        self.assertEqual(row['e2e_ms'], row['ready_e2e_ms'])
        self.assertEqual(row['total_wall_ms'], 400)

    def test_four_batches_of_sixteen_keep_counts_and_release_each_sandbox(self):
        row, state = self.run_e2b(64)
        self.assertTrue(row['success'])
        self.assertEqual(row['success_count'], 64)
        self.assertEqual(row['batch_size'], 16)
        self.assertEqual([batch['count'] for batch in row['batches']], [16] * 4)
        self.assertEqual(len(row['children']), 64)
        self.assertEqual(len(row['child_creates']), 64)
        self.assertCountEqual(state['kills'], ['source'] + [f'child-{i}' for i in range(64)])
        self.assertEqual(sum(entry['phase'] == 'inter-batch' for entry in row['cleanup']), 48)
        self.assertEqual(sum(entry['phase'] == 'final' for entry in row['cleanup']), 18)

if __name__ == '__main__': unittest.main()
