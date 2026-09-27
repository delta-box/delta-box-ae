"""No-VM tests for original dual-mock monotonic synchronization and cleanup."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'ae/vendor/finalbench/e2b_finalbench/e2b_paper_nested_driver.py'


def module():
    names = ('http_json', 'free_tcp_port', 'resolve_trajectory_path', 'start_shared_mock',
             'stop_proc', 'start_external_index_sidecar', 'make_payload_tar',
             'load_initial_tree', 'controller_tree_from_dict', 'controller_build_action_only',
             'flush_audit', 'message_policy')
    common = SimpleNamespace(**{k: mock.Mock(name=k) for k in names},
                             BASE=Path('/unused'), SOURCE_REPOS=Path('/unused/repos'),
                             INDEX_STORE=Path('/unused/index'), DEFAULT_TRACES_ROOT=Path('/unused/traces'))
    spec = importlib.util.spec_from_file_location('paper_dual_under_test', SOURCE)
    value = importlib.util.module_from_spec(spec)
    with mock.patch.dict('sys.modules', {'e2b_slim_finalbench_pilot': common}):
        spec.loader.exec_module(value)
    return value


def completion(node, purpose):
    return SimpleNamespace(node_id=node, purpose=purpose, dur_s=0)


def stats(cursor, served, total=4):
    return dict(ok=True, cursor=cursor, n_served=served, total=total,
                message_policy='strict', n_mismatch=0, n_protocol_errors=0)


class DualMockTests(unittest.TestCase):
    def setUp(self):
        self.driver = module()
        self.sequence = [completion(30, 'build_action'), completion(30, 'exec.FindClass'),
                         completion(31, 'build_action'), completion(32, 'build_action')]

    def test_forward_worker_sync_proves_only_build_calls_skipped(self):
        d = self.driver
        with mock.patch.object(d, 'checked_stats', side_effect=[stats(0, 0), stats(1, 0)]),              mock.patch.object(d, 'http_json', return_value={'ok': True, 'cursor': 1}) as http:
            proof = d.sync_forward(1234, 1, self.sequence, purpose='build_action')
        self.assertEqual(proof['skipped_count'], 1)
        self.assertEqual(http.call_args.kwargs['obj'], {'cursor': 1})

    def test_forward_controller_sync_matches_actual_exec_interval(self):
        d = self.driver
        with mock.patch.object(d, 'checked_stats', side_effect=[stats(1, 1), stats(2, 1)]),              mock.patch.object(d, 'http_json', return_value={'ok': True, 'cursor': 2}):
            proof = d.sync_forward(1234, 2, self.sequence, purpose='exec', node_id=30)
        self.assertEqual(proof['skipped_count'], 1)
        self.assertEqual(proof['direction'], 'forward-only')

    def test_noop_does_not_issue_rewind(self):
        d = self.driver
        with mock.patch.object(d, 'checked_stats', side_effect=[stats(1, 1), stats(1, 1)]),              mock.patch.object(d, 'http_json') as http:
            d.sync_forward(1234, 1, self.sequence, purpose='exec', node_id=30)
        http.assert_not_called()

    def test_backward_out_of_bounds_bool_and_wrong_purpose_rejected(self):
        d = self.driver
        for cursor, target, purpose, node in [(2, 1, 'exec', 30), (0, 5, 'build_action', None),
                                             (0, True, 'build_action', None), (0, 2, 'build_action', None),
                                             (1, 2, 'exec', 99)]:
            with self.subTest(target=target, purpose=purpose, node=node),                  mock.patch.object(d, 'checked_stats', return_value=stats(cursor, 0)),                  mock.patch.object(d, 'http_json') as http, self.assertRaises(RuntimeError):
                d.sync_forward(1234, target, self.sequence, purpose=purpose, node_id=node)
            http.assert_not_called()

    def test_sync_requires_ack_and_preserves_actual_served_count(self):
        d = self.driver
        for response, after in [({'ok': False, 'cursor': 1}, stats(1, 0)),
                                ({'ok': True, 'cursor': True}, stats(1, 0)),
                                ({'ok': True, 'cursor': 1}, stats(1, 1))]:
            with self.subTest(response=response, after=after),                  mock.patch.object(d, 'checked_stats', side_effect=[stats(0, 0), after]),                  mock.patch.object(d, 'http_json', return_value=response), self.assertRaises(RuntimeError):
                d.sync_forward(1234, 1, self.sequence, purpose='build_action')

    def test_consumption_requires_exact_cursor_delta_and_node(self):
        d = self.driver
        self.assertEqual(len(d.consumed(stats(0, 0), stats(1, 1), self.sequence,
                                        purpose='build_action', node_id=30)), 1)
        self.assertEqual(d.consumed(stats(2, 1), stats(2, 1), self.sequence,
                                    purpose='exec', node_id=30), [])
        for before, after, purpose, node in [(stats(0, 0), stats(0, 0), 'build_action', 30),
                                           (stats(0, 0), stats(1, 2), 'build_action', 30),
                                           (stats(0, 0), stats(2, 2), 'build_action', 30),
                                           (stats(1, 1), stats(2, 2), 'exec', 31)]:
            with self.subTest(before=before, after=after), self.assertRaises(RuntimeError):
                d.consumed(before, after, self.sequence, purpose=purpose, node_id=node)

    def test_stats_require_strict_policy_and_zero_protocol_failures(self):
        d = self.driver
        good = stats(0, 0)
        with mock.patch.object(d, 'http_json', return_value=good):
            self.assertEqual(d.checked_stats(1234, self.sequence), good)
        for key, value in [('message_policy', 'audit'), ('n_mismatch', 1),
                           ('n_protocol_errors', 1), ('n_served', True),
                           ('cursor', True), ('cursor', 5), ('total', 7), ('total', True),
                           ('n_mismatch', False), ('n_protocol_errors', False), ('ok', False)]:
            bad = dict(good, **{key: value})
            with self.subTest(key=key), mock.patch.object(d, 'http_json', return_value=bad), self.assertRaises(RuntimeError):
                d.checked_stats(1234, self.sequence)

    def test_second_mock_start_failure_flushes_and_stops_first(self):
        d = self.driver
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / 'trajectory.json';trace.write_text('{}')
            args = SimpleNamespace(instance='input', traces_root=directory, trace_variant='ms',
                                   run_id_prefix='test', worker_mock_port=0)
            first = object()
            with mock.patch.object(d, 'BASE', Path(directory) / 'driver'),                  mock.patch.object(d, 'RunContract'),                  mock.patch.object(d, 'resolve_trajectory_path', return_value=trace),                  mock.patch.object(d, 'free_tcp_port', side_effect=[1234, 1235]),                  mock.patch.object(d, 'start_shared_mock', side_effect=[first, RuntimeError('worker start failed')]),                  mock.patch.object(d, 'flush_audit') as flush,                  mock.patch.object(d, 'stop_proc') as stop,                  mock.patch.object(d, 'start_external_index_sidecar') as index, self.assertRaisesRegex(RuntimeError, 'worker start failed'):
                d.run_pilot(args, transport=object())
            index.assert_not_called()
            self.assertEqual(flush.call_args.args[0], 'http://127.0.0.1:1234')
            self.assertEqual(flush.call_args.args[1].name, 'controller_mock_audit.json')
            self.assertIn(mock.call(first), stop.call_args_list)

    def test_contract_rejects_missing_natural_termination_or_action(self):
        d = self.driver
        contract = object.__new__(d.RunContract)
        contract.expansions = [{}, {}]
        contract.actions = {(30, 0): {}}
        valid = [{'ok': True, 'e2b_steps': [object()]}, {'ok': True, 'finished': True, 'e2b_steps': []}]
        contract.final(valid)
        for bad in [valid[:1], [dict(valid[0]), dict(valid[1], finished=False)],
                    [dict(valid[0], e2b_steps=[]), valid[1]]]:
            with self.assertRaises(RuntimeError):
                contract.final(bad)

    def test_generated_guest_readiness_python_is_executable_source(self):
        import ast
        tree=ast.parse(SOURCE.read_text())
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign)
                  and any(isinstance(t,ast.Name) and t.id=='guest_check' for t in n.targets))
        expression=ast.fix_missing_locations(ast.Expression(body=node.value))
        source=eval(compile(expression,'<guest-check-expression>','eval'),
                    {'common':SimpleNamespace(SIDE_CAR_IP_FOR_SANDBOX='10.0.2.2'),
                     'worker_mock_port':1234,'index_port':1235})
        compile(source,'<guest-check>','exec')
        self.assertIn("assert len(cpus)==1",source)
        self.assertIn("mem['SwapTotal']==0",source)
        self.assertIn("paper-l2-proof.json",source)

    def test_fixed_parameters_and_fresh_base_rejected_before_input_reads(self):
        from ae.scripts import e2b_paper_profile as profile
        d = self.driver
        valid = dict(trace_variant='ms', max_steps=30, warm_action_worker=False,
                     clean_storage=False, root_build='', materialize_file_context=False,
                     mem_mib=2048, disk_mb=4096, fc_version='v1.14.1_458ca91',
                     from_build='fresh-id', instance='instance', storage='/owned/storage')
        transport = SimpleNamespace(storage='/owned/storage',
            manifest={'instance': 'instance', 'fresh_base_build_id': 'fresh-id'})
        bads = [('max_steps', 20), ('warm_action_worker', True), ('clean_storage', True),
                ('root_build', 'reused'), ('materialize_file_context', True),
                ('mem_mib', 8192), ('disk_mb', 1024), ('fc_version', 'newer'),
                ('from_build', 'other-id'), ('instance', 'other'), ('storage', '/foreign')]
        for key, value in bads:
            with self.subTest(key=key), mock.patch.object(profile, 'verify_inputs') as verify, self.assertRaises(ValueError):
                d.RunContract(SimpleNamespace(**dict(valid, **{key: value})), transport)
            verify.assert_not_called()

    def test_contract_binds_driver_trace_and_rtt_and_rejects_tamper(self):
        import hashlib
        from ae.scripts import e2b_paper_profile as profile
        d = self.driver
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / 'trajectory.json'
            rtt = trace.with_name('ms_trace.jsonl')
            raw = json.dumps({'root': {'node_id': 0, 'children': []}}).encode()
            trace.write_bytes(raw); rtt.write_bytes(b'RTT')
            proof = {'manifest': {'sha256': 'manifest'}, 'contract': {'path': '/contract'},
                     'inputs': [{'instance': 'instance', 'repository_commit': 'base',
                       'expansions': 29, 'actions': 26,
                       'trajectory': {'sha256': hashlib.sha256(raw).hexdigest()},
                       'rtt': {'sha256': hashlib.sha256(b'RTT').hexdigest()}}]}
            contract = {'inputs': [{'instance': 'instance', 'observed_expansions': []}],
                        'ordered_measured_actions': []}
            args = SimpleNamespace(trace_variant='ms', max_steps=30, warm_action_worker=False,
                     clean_storage=False, root_build='', materialize_file_context=False,
                     mem_mib=2048, disk_mb=4096, fc_version='v1.14.1_458ca91',
                     from_build='fresh-id', instance='instance', storage='/owned/storage',
                     traces_root=directory)
            transport = SimpleNamespace(storage='/owned/storage',
                manifest={'instance': 'instance', 'fresh_base_build_id': 'fresh-id'})
            with mock.patch.object(profile, 'verify_inputs', return_value=proof), mock.patch.object(profile, '_root_read', return_value=json.dumps(contract).encode()), mock.patch.object(d, 'resolve_trajectory_path', return_value=trace):
                bound = d.RunContract(args, transport)
                self.assertEqual(bound.binding['repository_commit'], 'base')
                self.assertFalse(bound.binding['expansion_guard']['original_cli_bound'])
                trace.write_bytes(raw + b' ')
                with self.assertRaisesRegex(ValueError, 'trajectory'):
                    d.RunContract(args, transport)
                trace.write_bytes(raw); rtt.write_bytes(b'changed RTT')
                with self.assertRaisesRegex(ValueError, 'RTT'):
                    d.RunContract(args, transport)

    def test_contract_rejects_wrong_parent_before_build(self):
        d = self.driver
        contract = object.__new__(d.RunContract)
        contract.expansions = [{'node_id': 30, 'parent_node_id': 0}]
        contract.before_build(1, 0, 30)
        for row in [(1, 2, 30), (1, 0, 31), (2, 30, 31)]:
            with self.assertRaises(RuntimeError):
                contract.before_build(*row)


if __name__ == '__main__':
    unittest.main()
