"""Audit policy, failure cleanup, and the parent-owned Replay timing boundary."""
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = ROOT / 'ae/vendor/spr_payload'
sys.path.insert(0, str(PAYLOAD))
import baseline_audit as audit


def load_driver(name, relative):
    source = ROOT / relative
    sys.path.insert(0, str(source.parent))
    env = {'SPR_PAYLOAD': str(PAYLOAD), 'MOATLESS_VENV': '/unused',
           'MOCK_TRACES_ROOT': '/unused', 'AE_BASE': str(source.parent),
           'AE_D_OVERLAY': '/unused', 'AE_KERNEL': '/unused', 'AE_BASE_XFS': '/unused',
           'PYTHONHASHSEED': '0', 'E2B_L1_KEY': '/unused'}
    with patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location(name, source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


REPLAY = load_driver('audit_replay', 'ae/vendor/finalbench/replay_copytree/real_trace_runner.py')
CRIU = load_driver('audit_criu', 'ae/vendor/finalbench/criu_copytree/criu_copytree_pilot.py')
FC = load_driver('audit_fc', 'ae/vendor/finalbench/fc_diff_dm/fc_dm_controller_pilot.py')
E2B = load_driver('audit_e2b', 'ae/vendor/finalbench/e2b_finalbench/e2b_slim_finalbench_pilot.py')
STANDALONE = load_driver('audit_standalone', 'ae/vendor/spr_payload/replay_driver.py')


TIMEOUT_DRIVER = r'''
import http.client,importlib.util,json,os,signal,sys,time
from pathlib import Path
source, directory, backend = sys.argv[1:]
sys.path.insert(0, str(Path(source).parent))
spec = importlib.util.spec_from_file_location('timeout_driver', source)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.PYTHON = Path(sys.executable)
module.HOST_IP = '127.0.0.1'
p = Path(directory)
import socket
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
base = f'http://127.0.0.1:{port}'
def interrupted(*args):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt('execute killpg timeout')
signal.signal(signal.SIGTERM, interrupted)
proc = None
def serve_mismatch_and_wait():
    (p/'identity.json').write_text(json.dumps({'driver_pid':os.getpid(),
         'driver_pgid':os.getpgrp(),'mock_pid':proc.pid,'mock_pgid':os.getpgid(proc.pid)}))
    request = {'messages':[{'role':'user','content':'different request'}]}
    if os.environ.get('TEST_INFLIGHT_RTT') == '1':
        client = http.client.HTTPConnection('127.0.0.1', port, timeout=90)
        client.request('POST','/v1/chat/completions',json.dumps(request),
                       {'Content-Type':'application/json'})
        # Do not wait for the 60-second recorded RTT. The supervisor expires
        # while the real single-threaded mock is still serving this request.
        (p/'inflight-started').touch()
    else:
        module.http_json(base+'/v1/chat/completions','POST',request)
        (p/'mismatch-served').touch()
    while True: time.sleep(.1)

if backend == 'standalone':
    module.http_json = module._http_json
    real_popen = module.subprocess.Popen
    def popen(*args, **kwargs):
        global proc
        proc = real_popen(*args, **kwargs)
        return proc
    module.subprocess.Popen = popen
    def replay(*args, **kwargs):
        module.http_json(base+'/admin/load','POST',{'instance_id':'test__repo-1','variant':'ms'})
        serve_mismatch_and_wait()
    module.run_replay = replay
    sys.argv = [source,'--manifest-line','test__repo-1__ms','--traces-root',os.environ['MOCK_TRACES_ROOT'],
                '--mock-port',str(port),'--audit-json',str(p/'mock_audit.json')]
    try:
        module.main()
    finally:
        if proc is not None:
            (p/'mock-exit.json').write_text(json.dumps({'returncode':proc.poll()}))
    raise SystemExit(0)
try:
    if backend == 'replay':
        proc = module.start_mock(port, p/'mock.log', p/'mock_audit.json')
        module.http_json(base+'/admin/load', 'POST', {'instance_id':'test__repo-1','variant':'ms'})
    else:
        proc = module.start_host_mock('test__repo-1', port, p/'mock.log', p/'mock_audit.json')
    serve_mismatch_and_wait()
finally:
    if proc is not None:
        primary = sys.exc_info()[1]
        # Ensure the process-group SIGTERM has reached every member before
        # flushing. On the old code this deterministically kills the mock.
        time.sleep(.15)
        try:
            module.flush_audit(base, p/'mock_audit.json', primary_error=primary)
        finally:
            (module.stop_proc if backend == 'replay' else module.stop_host_mock)(proc)
            (p/'mock-exit.json').write_text(json.dumps({'returncode':proc.poll()}))
'''


class OwnedMockTimeoutTests(unittest.TestCase):
    def test_execute_group_timeout_preserves_real_mock_mismatch_then_reaps_it(self):
        self.check_owned_timeout(inflight=False)

    def test_inflight_long_rtt_failure_has_bounded_audit_error_and_no_orphan(self):
        self.check_owned_timeout(inflight=True)

    def check_owned_timeout(self, inflight):
        from ae.repro.process import execute
        backends = [('replay', REPLAY), ('fc', FC)]
        if not inflight:
            backends.append(('standalone', STANDALONE))
        for backend, module in backends:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp)
                trajectory = p/'traces/qwen3-coder-30b-ms/test__repo-1/trajectory.json'
                trajectory.parent.mkdir(parents=True)
                trajectory.write_text(json.dumps({'root': {'node_id':0, 'completions': {
                    'build_action': {'input':[{'role':'user','content':'recorded request'}],
                                     'response':{'created':1700000060, 'id':'recorded-response'}}}}}))
                if inflight:
                    trajectory.with_name('ms_trace.jsonl').write_text(json.dumps({
                        't_wall_start_s':1700000000, 'dur_s':60.0})+'\n')
                python = p/'venv/bin/python'; python.parent.mkdir(parents=True)
                python.symlink_to(sys.executable)
                env = os.environ.copy()
                env.update(SPR_PAYLOAD=str(PAYLOAD), MOATLESS_VENV=str(p/'venv'),
                           MOCK_TRACES_ROOT=str(p/'traces'), AE_BASE=str(Path(module.__file__).parent),
                           AE_D_OVERLAY='/unused', AE_KERNEL='/unused', AE_BASE_XFS='/unused',
                           PYTHONHASHSEED='0', MOCK_MESSAGE_POLICY='audit',
                           TEST_INFLIGHT_RTT='1' if inflight else '0')
                result = execute([sys.executable, '-c', TIMEOUT_DRIVER, module.__file__, tmp, backend],
                                 p/'execute', cwd=ROOT, timeout=2.0, env=env)
                self.assertEqual(result['status'], 'failed')
                self.assertIn('TimeoutExpired', result['error'])
                self.assertTrue((p/('inflight-started' if inflight else 'mismatch-served')).exists(),
                                (p/'execute/stdout.log').read_text())
                identity = json.loads((p/'identity.json').read_text())
                self.assertNotEqual(identity['driver_pgid'], identity['mock_pgid'])
                self.assertEqual(identity['mock_pgid'], identity['mock_pid'])
                evidence = json.loads((p/'mock_audit.json').read_text())
                if inflight and backend != 'replay':
                    self.assertIn(evidence['audit_error']['type'], ('TimeoutError', 'timeout'))
                    self.assertLess(result['elapsed_s'], 15, 'cleanup exceeded safe supervisor margin')
                    self.assertGreater(result['elapsed_s'], 6, 'long RTT did not block audit export')
                else:
                    self.assertNotIn('audit_error', evidence)
                    self.assertEqual(evidence['stats']['n_mismatch'], 1)
                    if backend == 'replay':
                        self.assertEqual(evidence['latency_policy'], 'zero')
                        self.assertEqual(evidence['stats']['sleep_wall_s'], 0.0)
                    self.assertEqual(evidence['records'][0]['request_messages'][0]['content'], 'different request')
                self.assertIsNotNone(json.loads((p/'mock-exit.json').read_text())['returncode'])
                with self.assertRaises(ProcessLookupError):
                    os.kill(identity['mock_pid'], 0)


class E2BRewindTests(unittest.TestCase):
    def invoke(self, cursor, reply=None, error=None):
        node = SimpleNamespace(node_id=7)
        selected = SimpleNamespace(node_id=0)
        tree = Mock()
        tree.assert_runnable = Mock()
        tree.is_finished.return_value = False
        tree._select.return_value = selected
        tree._expand.return_value = node
        # No SDK/network/sandbox is involved: stop at the exact build-action
        # boundary and verify that invalid rewind never reaches that boundary.
        imports = {'moatless.actions.model': SimpleNamespace(Observation=Mock()),
                   'moatless.file_context': SimpleNamespace(FileContext=Mock())}
        kwargs = dict(tree=tree, instance='test__repo-1', seq=1, build_by_node={0:'root'},
                      node_build_cursor=cursor, completions=[], storage='/unused', work=Path('/unused'),
                      shared_mock_port=1, index_port=2, materialize_file_context=False,
                      warm_action_worker=False)
        with patch.dict(sys.modules, imports), \
                patch.object(E2B, 'http_json', return_value=reply, side_effect=error) as request, \
                patch.object(E2B, 'controller_build_action_only', side_effect=StopIteration('build boundary')) as build:
            if error is not None:
                with self.assertRaises(type(error)):
                    E2B.run_one_e2b_iteration(**kwargs)
                build.assert_not_called()
            elif reply == {'ok': True, 'cursor': 14} and cursor == {7:14}:
                with self.assertRaisesRegex(StopIteration, 'build boundary'):
                    E2B.run_one_e2b_iteration(**kwargs)
                build.assert_called_once_with(tree, node)
            else:
                with self.assertRaisesRegex(RuntimeError, 'cursor'):
                    E2B.run_one_e2b_iteration(**kwargs)
                build.assert_not_called()
            return request.call_count

    def test_missing_invalid_cursor_fails_before_http_or_build(self):
        for cursor in ({}, {7:None}, {7:-1}, {7:True}, {7:'14'}):
            with self.subTest(cursor=cursor):
                self.assertEqual(self.invoke(cursor), 0)

    def test_rewind_transport_rejection_missing_or_wrong_ack_prevents_build(self):
        self.assertEqual(self.invoke({7:14}, error=OSError('transport failed')), 1)
        for reply in ({'ok':False, 'cursor':14}, {'ok':True}, {'ok':True, 'cursor':13},
                      {'ok':True, 'cursor':'14'}, None, []):
            with self.subTest(reply=reply):
                self.assertEqual(self.invoke({7:14}, reply=reply), 1)
        self.assertEqual(self.invoke({7:14}, reply={'ok':True, 'cursor':14}), 2)


def response(policy='audit', mismatch=2, protocol=0):
    return {'ok': True, 'schema_version': 1, 'message_policy': policy, 'flush_id': 1,
            'stats': {'ok': True, 'cursor': 2, 'total': 5, 'n_mismatch': mismatch,
                      'n_protocol_errors': protocol, 'message_policy': policy},
            'records': [{'cursor': 1, 'type': 'messages_mismatch'}], 'buffer': {}}


class AuditClientTests(unittest.TestCase):
    def test_audit_messages_nonfatal_but_strict_protocol_and_cursor_fail(self):
        with patch.dict(os.environ, {'MOCK_MESSAGE_POLICY': 'audit'}):
            self.assertTrue(audit.stats_ok(response()['stats']))
            self.assertFalse(audit.stats_ok(response(protocol=1)['stats']))
            stats = response()['stats']; stats['cursor'] = 6
            self.assertFalse(audit.stats_ok(stats))
        with patch.dict(os.environ, {'MOCK_MESSAGE_POLICY': 'strict'}):
            self.assertFalse(audit.stats_ok(response(policy='strict')['stats']))
            self.assertTrue(audit.stats_ok(response(policy='strict', mismatch=0)['stats']))

    def test_flush_preserves_full_payload_and_records_transport_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MOCK_MESSAGE_POLICY': 'audit'}):
            output = Path(tmp) / 'mock_audit.json'
            payload = response()
            with patch.object(audit.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
                self.assertEqual(audit.flush_audit('http://mock', output), payload)
            self.assertEqual(json.loads(output.read_text()), payload)
            with patch.object(audit.urllib.request, 'urlopen', side_effect=OSError('server gone')):
                with self.assertRaisesRegex(RuntimeError, 'mock audit failed'):
                    audit.flush_audit('http://mock', output)
                audit.flush_audit('http://mock', output, primary_error=ValueError('workload'))
            self.assertEqual(json.loads(output.read_text())['audit_error']['message'], 'server gone')

    def test_export_error_does_not_mask_primary_but_success_cannot_pass(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MOCK_MESSAGE_POLICY': 'audit'}):
            output = Path(tmp) / 'mock_audit.json'
            def http(*args, **kwargs):
                return io.BytesIO(json.dumps(response()).encode())
            with patch.object(audit.urllib.request, 'urlopen', side_effect=http), \
                    patch.object(Path, 'write_text', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(RuntimeError, 'audit export failed'):
                    audit.flush_audit('http://mock', output)
                with patch('sys.stderr', new_callable=io.StringIO) as log:
                    audit.flush_audit('http://mock', output, primary_error=ValueError('original workload'))
                self.assertIn('original error retained: original workload', log.getvalue())


class ReplayTimingTests(unittest.TestCase):
    def run_restore(self, failure=False):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); src = p / 'sources' / 'swe-bench_test__repo-1'
            src.mkdir(parents=True); (src / 'file').write_text('real file')
            output = p / 'results'; output.mkdir()
            clock, order = [0.0], []

            def subprocess_run(*args, **kwargs):
                order.append('subprocess'); clock[0] += 7
                if failure:
                    raise subprocess.TimeoutExpired('replay', 900)
                (output / 'restore_000.driver.json').write_text(json.dumps({
                    'ok': True, 'status': 'TARGET_REACHED', 'mock_stats': {
                        **response()['stats'], 'sleep_wall_s': 0.0, 'latency_policy': 'zero'}}))
                return Mock(returncode=0)

            def flush(*args, **kwargs):
                order.append('flush'); clock[0] += 100
                self.assertEqual(bool(kwargs.get('primary_error')), failure)

            with patch.object(REPLAY, 'BASE', p), patch.object(REPLAY, 'SOURCE_REPOS', p/'sources'), \
                    patch.object(REPLAY, 'start_mock', return_value=Mock()), \
                    patch.object(REPLAY, 'free_tcp_port', return_value=1234), \
                    patch.object(REPLAY.subprocess, 'run', side_effect=subprocess_run), \
                    patch.object(REPLAY.time, 'perf_counter', side_effect=lambda: clock[0]), \
                    patch.object(REPLAY, 'flush_audit', side_effect=flush), \
                    patch.object(REPLAY, 'stop_proc', side_effect=lambda proc: order.append('stop')):
                event = {'target_expansions': 1, 'iter': 2, 'target_node': 1, 'target_cursor_inclusive': 0}
                if failure:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        REPLAY.run_one_restore('test__repo-1', event, 0, output, False)
                else:
                    row = REPLAY.run_one_restore('test__repo-1', event, 0, output, False)
                    self.assertEqual(row['replay_ms'], 7000)
                    self.assertEqual(row['restore_zero_llm_ms'], 7000)
                    self.assertEqual(row['replay_timing_method'], 'zero-latency-wall')
            self.assertEqual(order, ['subprocess', 'flush', 'stop'])
            self.assertFalse((p / 'workdir/real/test__repo-1/restore_000').exists())

    def test_parent_excludes_slow_flush_but_keeps_whole_subprocess(self):
        self.run_restore()

    def test_timeout_still_flushes_before_stop_and_preserves_error(self):
        self.run_restore(failure=True)


class PilotCleanupTests(unittest.TestCase):
    def test_criu_worker_start_failure_still_flushes_before_mock_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []; failure = ValueError('worker init failed')
            with patch.object(CRIU, 'BASE', Path(tmp)), \
                    patch.object(Path, 'read_text', return_value='0'), \
                    patch.object(CRIU, 'rsync_copy', return_value={'rc': 0}), \
                    patch.object(CRIU, 'free_tcp_port', return_value=1234), \
                    patch.object(CRIU, 'start_mock', return_value=Mock()), \
                    patch.object(CRIU, 'start_worker', side_effect=failure), \
                    patch.object(CRIU, 'kill_pid', side_effect=lambda pid: order.append('worker stop')), \
                    patch.object(CRIU, 'flush_audit', side_effect=lambda *a, **kw: order.append(('flush', kw['primary_error']))), \
                    patch.object(CRIU, 'stop_proc', side_effect=lambda proc: order.append('mock stop')):
                with self.assertRaisesRegex(ValueError, 'worker init failed'):
                    CRIU.run_pilot('test__repo-1', max_steps=1, run_id_prefix='test',
                                   cleanup_large_artifacts=False, numa_node=0)
            self.assertEqual(order, ['worker stop', ('flush', failure), 'mock stop'])

    def test_fc_failure_or_flush_failure_always_stops_mock_and_tears_down(self):
        for body_failure in (True, False):
            with self.subTest(body_failure=body_failure), tempfile.TemporaryDirectory() as tmp:
                order = []
                vm, dm = Mock(), Mock()
                if body_failure:
                    vm.spawn.side_effect = ValueError('VM startup failed')
                vm.kill.side_effect = lambda: order.append('vm stop')
                vm.take_snapshot.return_value = {'fc_total_ms': 1.0}
                dm.snapshot.return_value = {'dm_snapshot_ms': 1.0}
                dm.teardown.side_effect = lambda: order.append('dm teardown')

                def flush(*args, **kwargs):
                    order.append('flush')
                    if not body_failure:
                        raise RuntimeError('audit transport failed')
                    self.assertIsInstance(kwargs['primary_error'], ValueError)

                with patch.object(FC, 'BASE', Path(tmp)), patch.object(FC, 'WORK_BASE', Path(tmp)/'work'), \
                        patch.object(Path, 'read_text', return_value='0'), \
                        patch.object(FC, 'prepare_rootfs'), patch.object(FC, 'setup_tap'), \
                        patch.object(FC, 'DMThin', return_value=dm), \
                        patch.object(FC, 'start_host_mock', return_value=Mock()), \
                        patch.object(FC, 'FirecrackerVM', return_value=vm), \
                        patch.object(FC, 'wait_state'), \
                        patch.object(FC, 'http_json', return_value={'ok': True, 'tree': {}, 'state': {}}), \
                        patch.object(FC, 'flush_audit', side_effect=flush), \
                        patch.object(FC, 'stop_host_mock', side_effect=lambda proc: order.append('mock stop')), \
                        patch.object(FC, 'run'):
                    with self.assertRaisesRegex((ValueError, RuntimeError), 'failed'):
                        FC.run_controller_pilot('test__repo-1', max_steps=0, mem_mib=1, vcpus=1, data_size='1G')
                self.assertEqual(order, ['vm stop', 'flush', 'mock stop', 'dm teardown'])


if __name__ == '__main__':
    unittest.main()
