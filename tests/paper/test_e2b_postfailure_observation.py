"""Post-failure observation must be bounded, owned, and unable to change the result."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('e2b_failure_evidence_fixture',
    Path(__file__).with_name('test_e2b_fanout_failure_evidence.py'))
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)
D = FIXTURE.DRIVER


class PostFailureObservationTests(unittest.TestCase):
    def invoke(self, sdk, forks='16'):
        return FIXTURE.FanoutFailureEvidenceTests().invoke(sdk, forks)

    def test_original_error_cleanup_and_exit_survive_observer_failure(self):
        sdk = FIXTURE.FakeSDK(failure='transport')
        with patch.object(D, 'observe_e2b_failure', side_effect=PermissionError('fixture')) as observe:
            rc, rows = self.invoke(sdk)
        self.assertEqual(rc, 1)
        self.assertEqual(rows[0]['error'], 'RuntimeError: inherited-memory verification failed')
        self.assertEqual(rows[0]['post_failure_observation'], {'error': 'PermissionError', 'extra_budget_seconds':10})
        self.assertGreaterEqual(rows[0]['post_failure_observation_wall_ms'], 0)
        self.assertEqual(len(sdk.verifies), 16)
        self.assertEqual(len(sdk.kills), 18)
        observe.assert_called_once()

    def test_success_and_create_failure_do_not_observe(self):
        with patch.object(D, 'observe_e2b_failure') as observe:
            rc, rows = self.invoke(FIXTURE.FakeSDK(), '1,4,16,64')
            self.assertEqual(rc, 0)
            self.assertTrue(all('post_failure_observation' not in r for r in rows))
            self.invoke(FIXTURE.FakeSDK(failure='create'))
        observe.assert_not_called()

    def test_observation_is_after_failed_verify_before_cleanup(self):
        sdk = FIXTURE.FakeSDK(failure='transport')
        def observe(*args):
            self.assertEqual(len(sdk.verifies), 16)
            self.assertEqual(sdk.kills, [])
            time.sleep(.02)
            return {'fixture': True}
        with patch.object(D, 'observe_e2b_failure', side_effect=observe):
            _, rows = self.invoke(sdk)
        row = rows[0]
        self.assertGreaterEqual(row['post_failure_observation_wall_ms'], 20)
        self.assertGreaterEqual(row['total_wall_ms'], row['post_failure_observation_wall_ms'])
        self.assertNotIn('ready_e2e_ms', row)
        self.assertEqual(len(row['batches']), 1)

    def test_hard_timeout_kills_and_reaps_subprocess(self):
        result = D._bounded_observation('import time; time.sleep(30)', {}, .1)
        self.assertEqual(result['status'], 'timeout')
        self.assertLess(result['wall_ms'], 3000)
        self.assertLess(result['returncode'], 0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(result['pid'], os.WNOHANG)

    def test_guest_selection_budget_and_secrets_not_in_receipt(self):
        children = [types.SimpleNamespace(sandbox_id=name, sandbox_domain='localhost',
            _envd_version='0.6.1', _envd_access_token='private-token',
            connection_config=types.SimpleNamespace(sandbox_headers={'E2b-Sandbox-Id':name,
                'E2b-Sandbox-Port':'49983', 'X-Access-Token':'private-token'})) for name in ('failed', 'passed')]
        prior = {'sandbox_id':'alreadyremoved', 'verify':{'ok':True}}
        rows = [prior, {'sandbox_id':'failed', 'verify':{'ok':False}},
                {'sandbox_id':'passed', 'verify':{'ok':True}}]
        calls = []
        def probe(code, payload, timeout):
            calls.append((code, payload, timeout))
            return {'status':'observed'}
        with patch.object(D, '_bounded_observation', side_effect=probe):
            result = D.observe_e2b_failure(children, rows, types.SimpleNamespace(out='/owned/job/fanout.json'),
                                         {'api_key':'private-api-key'})
        self.assertEqual([c[2] for c in calls], [4, 4, 2])
        self.assertEqual([g['sandbox_id'] for g in result['guests']], ['failed', 'passed'])
        self.assertEqual(result['extra_budget_seconds'], 10)
        self.assertNotIn('private-token', json.dumps(result))
        self.assertNotIn('private-api-key', json.dumps(result))
        for index, child in enumerate(children):
            headers = calls[index][1]['options']['extra_sandbox_headers']
            self.assertEqual(headers, child.connection_config.sandbox_headers)
            self.assertIsNot(headers, child.connection_config.sandbox_headers)
        self.assertNotIn('connect(', D._GUEST_OBSERVATION_CODE)
        self.assertNotIn('sendall', D._GUEST_OBSERVATION_CODE)
        self.assertNotIn('touch_buffer', D._GUEST_OBSERVATION_CODE)

    def test_all_failed_batch_selects_one_guest_and_six_second_budget(self):
        rows = [{'sandbox_id':'failed', 'verify':{'ok':False}}]
        child = types.SimpleNamespace(sandbox_id='failed', sandbox_domain='localhost',
            _envd_version='0.6.1', _envd_access_token=None,
            connection_config=types.SimpleNamespace(sandbox_headers={}))
        with patch.object(D, '_bounded_observation', return_value={}) as probe:
            result = D.observe_e2b_failure([child], rows, types.SimpleNamespace(out='/owned/fanout.json'), {})
        self.assertEqual(result['extra_budget_seconds'], 6)
        self.assertEqual(probe.call_count, 2)

    def test_sdk_observer_constructs_client_without_connect_or_create(self):
        bootstrap = '''
import json, sys, types
class Config:
    def __init__(self, **options): assert options['request_timeout'] == 2
class Sandbox:
    def __init__(self, **options):
        assert options['sandbox_id'] == 'existing'
        assert options['envd_access_token'] == 'private-token'
        self.commands = self
    def run(self, command, *, timeout, request_timeout):
        assert timeout == request_timeout == 2
        return types.SimpleNamespace(exit_code=0, stdout=json.dumps({'read_only_probe': True}))
    def connect(self, *args, **kwargs): raise AssertionError('connect changes lifetime')
    def create(self, *args, **kwargs): raise AssertionError('must not create a sandbox')
sys.modules['e2b'] = types.SimpleNamespace(Sandbox=Sandbox)
sys.modules['e2b.connection_config'] = types.SimpleNamespace(ConnectionConfig=Config)
sys.modules['packaging'] = types.ModuleType('packaging')
sys.modules['packaging.version'] = types.SimpleNamespace(Version=str)
'''
        payload = {'id':'existing', 'domain':'localhost', 'version':'0.6.1',
                   'envd_token':'private-token', 'traffic_token':None,
                   'options':{'request_timeout':2}, 'command':'read-only-fixture'}
        result = D._bounded_observation(bootstrap + D._SDK_OBSERVATION_CODE, payload, 4)
        self.assertEqual(result['result'], {'exit_code':0, 'observation':{'read_only_probe':True}})
        self.assertNotIn('private-token', json.dumps(result))

    def test_host_rejects_reused_daemon_pid_and_unrelated_vm_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); proc = root/'proc'; groups = root/'e2b'
            group = groups/'sbx-owned-a'; group.mkdir(parents=True)
            (group/'cgroup.procs').write_text('101\n102\n')
            for pid, start, cgroup in [(101, 900, '0::/e2b/sbx-owned-a'),
                                       (102, 901, '0::/system.slice/unrelated.service'),
                                       (103, 999, '0::/system.slice/ae-e2b-orchestrator.service')]:
                path = proc/str(pid); path.mkdir(parents=True)
                (path/'stat').write_text(str(pid)+' (fixture) '+' '.join(['S']+['0']*18+[str(start)]))
                (path/'cgroup').write_text(cgroup+'\n')
            context = root/'context.json'
            context.write_text(json.dumps({'ae-e2b-orchestrator.service':
                {'pid':103, 'start_ticks':998, 'cgroup':'0::/system.slice/ae-e2b-orchestrator.service'}}))
            code = D._HOST_OBSERVATION_CODE.replace("Path('/proc')", 'Path('+repr(str(proc))+')')
            code = code.replace("Path('/sys/fs/cgroup/e2b')", 'Path('+repr(str(groups))+')')
            result = D._bounded_observation(code, {'ids':['owned'], 'context':str(context)}, 2)['result']
            self.assertEqual(result['vms'][0]['processes'][0]['ownership'], 'owned-sandbox-cgroup')
            self.assertEqual(result['vms'][0]['processes'][1]['ownership'], 'unknown')
            self.assertEqual(result['daemon']['ownership'], 'unknown')
            self.assertNotIn('tasks', result['daemon'])

    def test_identity_change_during_sample_never_gets_owned_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); proc = root/'proc'; groups = root/'e2b'
            group = groups/'sbx-owned-a'; group.mkdir(parents=True)
            (group/'cgroup.procs').write_text('101\n')
            for pid, start, cgroup in [(101, 900, '0::/e2b/sbx-owned-a'),
                                       (103, 999, '0::/system.slice/ae-e2b-orchestrator.service')]:
                path = proc/str(pid); path.mkdir(parents=True)
                (path/'stat').write_text(str(pid)+' (fixture) '+' '.join(['S']+['0']*18+[str(start)]))
                (path/'cgroup').write_text(cgroup+'\n')
            context = root/'context.json'
            context.write_text(json.dumps({'ae-e2b-orchestrator.service':
                {'pid':103, 'start_ticks':999, 'cgroup':'0::/system.slice/ae-e2b-orchestrator.service'}}))
            code = D._HOST_OBSERVATION_CODE.replace("Path('/proc')", 'Path('+repr(str(proc))+')')
            code = code.replace("Path('/sys/fs/cgroup/e2b')", 'Path('+repr(str(groups))+')')
            # Reuse each fixture PID exactly between the before/after identity reads.
            code = code.replace("    result['schedstat'] =", "    (path/'stat').write_text((path/'stat').read_text().replace('900', '901').replace('999', '1000'))\n    result['schedstat'] =")
            result = D._bounded_observation(code, {'ids':['owned'], 'context':str(context)}, 2)['result']
            for row in [result['vms'][0]['processes'][0], result['daemon']]:
                self.assertFalse(row['identity_stable'])
                self.assertEqual(row['ownership'], 'unknown')
                self.assertNotEqual(row['start_ticks'], row['identity_after']['start_ticks'])


if __name__ == '__main__':
    unittest.main()
