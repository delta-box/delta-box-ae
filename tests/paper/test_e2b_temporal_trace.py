"""Original request stages and failures stay intact under opt-in diagnostic tracing."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import types
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('e2b_trace_fixture',
    Path(__file__).with_name('test_e2b_fanout_failure_evidence.py'))
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)
D = FIXTURE.DRIVER


class Connection:
    def __init__(self, reply=b'OK token=fixture-token checksum=1 bytes=1\n', failure=None):
        self.reply, self.failure, self.calls = reply, failure, []
    def getsockname(self): return ('127.0.0.1', 50123)
    def getpeername(self): return ('127.0.0.1', 38765)
    def sendall(self, data): self.calls.append(('sendall', data))
    def recv(self, count):
        self.calls.append(('recv', count))
        if self.failure is not None: raise self.failure
        return self.reply
    def close(self): self.calls.append(('close',))
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class TemporalTraceTests(unittest.TestCase):
    def test_default_off_is_byte_identical_to_source_f661(self):
        expected = {'server':'ee905a903ac311bc9a07e5713c5f02945154a394044c61b53cddfadfde6fe682',
            'start':'606bd7a020b9ab0aa0db75e0d21abdf6917cc6edb1766458a199f83c35fcdcce',
            'verify':'2fafae6acbd14f78903ed5774e9323a1d4586c8869a64e6b448260e614a0c12b',
            'guest':'960ee18f3c6c41e7be0f72e1caeed387051f1241779344fd3f3ade5f03189fe8'}
        for flag in (None, '', '0', 'true'):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop('DELTABOX_E2B_DIAGNOSTIC_TRACE', None)
                if flag is not None: os.environ['DELTABOX_E2B_DIAGNOSTIC_TRACE'] = flag
                values = {'server':D.MEM_SERVER_CODE,
                    'start':D.start_mem_server_shell(mem_mib=64, token='fixture-token'),
                    'verify':D.verify_mem_server_shell(token='fixture-token'), 'guest':D.guest_observation_code()}
                self.assertEqual({k:hashlib.sha256(v.encode()).hexdigest() for k,v in values.items()}, expected)

    def run_client(self, directory, connection, *, trace_io_fails=False):
        with patch.dict(os.environ, {'DELTABOX_E2B_DIAGNOSTIC_TRACE':'1'}):
            shell = D.verify_mem_server_shell(token='fixture-token')
        code = shell.split("python3 - <<'PY'\n",1)[1].rsplit('\nPY\n',1)[0]
        code = code.replace('/tmp/official_fork_', str(directory / 'official_fork_'))
        (directory/'official_fork_state.txt').write_text('official-fork-state token=fixture-token')
        if trace_io_fails:
            code = code.replace("with open(_trace_path, 'ab') as stream:", "with open('/no-such-trace-parent/file', 'ab') as stream:")
        def connect(address, timeout):
            connection.calls.append(('connect', address, timeout));return connection
        with patch.object(socket, 'create_connection', side_effect=connect), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(code, '<original-verifier-with-trace>', 'exec'), {})

    def test_client_original_protocol_order_and_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp); c=Connection();self.run_client(p,c)
            self.assertEqual(c.calls, [('connect',('127.0.0.1',38765),10),('sendall',b'touch\n'),('recv',4096),('close',)])
            records=[json.loads(line) for line in (p/'official_fork_client_trace.jsonl').read_text().splitlines()]
            self.assertEqual([x['stage'] for x in records], ['connect_start','connect_end','send_start','send_end','recv_start','recv_end','success','exit'])
            self.assertTrue(all(x['phase']=='verify' and x['token']=='fixture-token' for x in records))
            self.assertTrue(all(x['monotonic_ns']<=y['monotonic_ns'] for x,y in zip(records,records[1:])))

    def test_original_recv_exception_survives_and_no_new_socket_close(self):
        for failed_trace in (False,True):
            with tempfile.TemporaryDirectory() as temp:
                p=Path(temp);primary=TimeoutError('original-deadline');c=Connection(failure=primary)
                with self.assertRaises(TimeoutError) as caught:self.run_client(p,c,trace_io_fails=failed_trace)
                self.assertIs(caught.exception,primary)
                self.assertEqual(c.calls[-1],('recv',4096));self.assertNotIn(('close',),c.calls)
                if not failed_trace:
                    records=[json.loads(line) for line in (p/'official_fork_client_trace.jsonl').read_text().splitlines()]
                    self.assertEqual([x['stage'] for x in records][-3:],['recv_start','exception','exit'])
                    self.assertEqual(records[-2]['error'],'TimeoutError')
                    self.assertNotIn('recv_end',[x['stage'] for x in records])

    def test_server_one_touch_same_reply_warmup_vs_verify(self):
        original=ast.parse(D.MEM_SERVER_CODE);traced=ast.parse(D.diagnostic_mem_server_code())
        self.assertEqual(ast.dump(next(n for n in original.body if isinstance(n,ast.FunctionDef) and n.name=='touch_buffer')),
            ast.dump(next(n for n in traced.body if isinstance(n,ast.FunctionDef) and n.name=='touch_buffer')))
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'OFFICIAL_FORK_TOKEN':'fixture-token','OFFICIAL_FORK_MEM_MIB':'1'}):
            p=Path(temp);code=D.diagnostic_mem_server_code().split('\nsock = socket.socket',1)[0]
            code=code.replace('/tmp/official_fork_',str(p/'official_fork_'));ns={};exec(code,ns)
            calls=[];ns['touch_buffer']=lambda:calls.append('touch') or 17
            for payload in (b'warmup\n',b'touch\n'):
                c=Connection(reply=payload);ns['handle'](c)
                self.assertEqual(c.calls[0],('recv',1024));self.assertIn(b'checksum=17',c.calls[1][1]);self.assertEqual(c.calls[-1],('close',))
            self.assertEqual(calls,['touch','touch'])
            records=[json.loads(line) for line in (p/'official_fork_server_trace.jsonl').read_text().splitlines()]
            self.assertEqual([r['phase'] for r in records if r['stage']=='recv_end'],['warmup','verify'])
            self.assertEqual(sum(r['stage']=='touch_start' for r in records),2)
            self.assertEqual(sum(r['stage']=='touch_end' for r in records),2)

    def test_caps_are_whole_json_with_explicit_incomplete_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'trace';ns={'os':os,'_trace_path':str(p),'_trace_token':'fixture-token'}
            exec(D._TEMPORAL_TRACE_HELPER,ns)
            ns['_trace']('huge',detail='x'*1000)
            for _ in range(100):ns['_trace']('event')
            lines=p.read_bytes().splitlines(keepends=True)
            self.assertEqual(len(lines),64);self.assertTrue(all(len(x)<=512 for x in lines))
            records=[json.loads(x) for x in lines]
            self.assertEqual(records[0]['reason'],'record_bytes');self.assertEqual(records[-1]['reason'],'record_limit')
            self.assertEqual([r['seq'] for r in records],list(range(64)))

    def test_server_touch_exception_remains_original_and_closes_original_context(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'OFFICIAL_FORK_TOKEN':'fixture-token','OFFICIAL_FORK_MEM_MIB':'1'}):
            p=Path(temp);code=D.diagnostic_mem_server_code().split('\nsock = socket.socket',1)[0]
            code=code.replace('/tmp/official_fork_',str(p/'official_fork_'));ns={};exec(code,ns)
            primary=ValueError('original-touch-failure')
            def touch():raise primary
            ns['touch_buffer']=touch;c=Connection(reply=b'touch\n')
            with self.assertRaises(ValueError) as caught:ns['handle'](c)
            self.assertIs(caught.exception,primary);self.assertEqual(c.calls,[('recv',1024),('close',)])
            records=[json.loads(line) for line in (p/'official_fork_server_trace.jsonl').read_text().splitlines()]
            self.assertEqual([x['stage'] for x in records],['recv_start','recv_end','touch_start','exception','exit'])

    def test_postfailure_context_is_bounded_port_filtered_and_trace_reads_last(self):
        requests=[]
        def read(path,limit=4096):
            requests.append((str(path),limit))
            if str(path)=='/proc/net/tcp':
                return {'text':'header\n 0: 0100007F:976D 0100007F:C3CB 01 a\n 1: 0100007F:0050 0100007F:0000 0A b\n','truncated':True}
            return {'error':'FileNotFoundError'}
        ns={'result':{},'read':read,'Path':Path,'os':os}
        exec(D._GUEST_TEMPORAL_OBSERVATION,ns)
        tcp=ns['result']['network_context']['/proc/net/tcp']
        self.assertIn('976D',tcp['text']);self.assertNotIn('0050',tcp['text']);self.assertTrue(tcp['truncated'])
        self.assertEqual(requests[-2:],[('/tmp/official_fork_server_trace.jsonl',32768),('/tmp/official_fork_client_trace.jsonl',32768)])
        self.assertTrue(all(limit<=32768 for _,limit in requests))
        self.assertEqual(ns['result']['temporal_traces']['server'],{'error':'FileNotFoundError'})

    def test_failure_observer_opt_in_keeps_original_rpc_count_and_budgets(self):
        child=types.SimpleNamespace(sandbox_id='failed',sandbox_domain='localhost',_envd_version='0.6.1',_envd_access_token=None,
            connection_config=types.SimpleNamespace(sandbox_headers={}))
        rows=[{'sandbox_id':'failed','verify':{'ok':False}}]
        with patch.dict(os.environ,{'DELTABOX_E2B_DIAGNOSTIC_TRACE':'1'}), patch.object(D,'_bounded_observation',return_value={}) as probe:
            D.observe_e2b_failure([child],rows,types.SimpleNamespace(out='/owned/fanout.json'),{})
        self.assertEqual([c.args[2] for c in probe.call_args_list],[4,2])
        command=probe.call_args_list[0].args[1]['command']
        self.assertIn('temporal_traces',command);self.assertNotIn('sendall',command);self.assertNotIn('create_connection',command)
        self.assertEqual(command.count('print(json.dumps(result))'),1)

    def test_diagnostic_row_cannot_be_mistaken_for_paper_measurement(self):
        with patch.dict(os.environ,{'DELTABOX_E2B_DIAGNOSTIC_TRACE':'1'}):
            rc, rows=FIXTURE.FanoutFailureEvidenceTests().invoke(FIXTURE.FakeSDK(),'1')
        self.assertEqual(rc,0)
        self.assertEqual(rows[0]['diagnostic_temporal_trace']['scope'],'diagnostic only; not paper acceptance')


if __name__=='__main__': unittest.main()
