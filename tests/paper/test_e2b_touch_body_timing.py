"""Opt-in body timing excludes trace I/O and never changes the original touch result."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('e2b_body_timing_fixture',
    Path(__file__).with_name('test_e2b_temporal_trace.py'))
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)
D = FIXTURE.D


class TouchBodyTimingTests(unittest.TestCase):
    def server(self, root):
        code = D.diagnostic_mem_server_code().split('\nsock = socket.socket', 1)[0]
        code = code.replace('/tmp/official_fork_', str(root / 'official_fork_'))
        ns = {}
        with patch.dict(os.environ, {'OFFICIAL_FORK_TOKEN':'fixture-token','OFFICIAL_FORK_MEM_MIB':'1'}):
            exec(code, ns)
        return ns

    def test_five_default_off_outputs_match_archived_0327_bytes(self):
        expected = ['ee905a903ac311bc9a07e5713c5f02945154a394044c61b53cddfadfde6fe682',
            '606bd7a020b9ab0aa0db75e0d21abdf6917cc6edb1766458a199f83c35fcdcce',
            '2fafae6acbd14f78903ed5774e9323a1d4586c8869a64e6b448260e614a0c12b',
            '960ee18f3c6c41e7be0f72e1caeed387051f1241779344fd3f3ade5f03189fe8',
            '2c08854c66dc572468db4b2cc594644ab93610891d69d740c9df682323b324c8']
        with patch.dict(os.environ, {'DELTABOX_E2B_DIAGNOSTIC_TRACE':'0'}):
            values = [D.MEM_SERVER_CODE, D.start_mem_server_shell(mem_mib=64,token='fixture-token'),
                D.verify_mem_server_shell(token='fixture-token'), D.guest_observation_code(), D._SDK_OBSERVATION_CODE]
        self.assertEqual([hashlib.sha256(v.encode()).hexdigest() for v in values], expected)

    def test_clock_order_body_once_and_trace_io_outside_bracket(self):
        with tempfile.TemporaryDirectory() as directory:
            ns = self.server(Path(directory)); events=[];state={'mono':0,'cpu':0};records=[]
            def trace(stage,*args,**fields):
                events.append(stage);records.append((stage,fields))
                if stage in ('touch_start','touch_end'):state['mono']+=100000
            def mono():events.append('mono');return state['mono']
            def cpu():events.append('thread_cpu');return state['cpu']
            def touch():events.append('touch');state['mono']+=100;state['cpu']+=20;return 17
            ns.update(_trace=trace,time=types.SimpleNamespace(monotonic_ns=mono,thread_time_ns=cpu),touch_buffer=touch)
            connection=FIXTURE.Connection(reply=b'touch\n');ns['handle'](connection)
            start=events.index('touch_start')
            self.assertEqual(events[start:start+7],['touch_start','mono','thread_cpu','touch','thread_cpu','mono','touch_end'])
            self.assertEqual(events.count('touch'),1)
            end=dict(records)['touch_end']
            self.assertEqual(end,{'body_mono_ns':[100000,100100],'body_thread_ns':[0,20],'body_timing':'captured'})
            self.assertIn(b'checksum=17',connection.calls[1][1]);self.assertEqual(connection.calls[-1],('close',))

    def test_clock_failure_is_unknown_without_changing_success(self):
        with tempfile.TemporaryDirectory() as directory:
            ns=self.server(Path(directory));records=[];touches=[]
            def unavailable():raise OSError('clock unavailable')
            ns.update(time=types.SimpleNamespace(monotonic_ns=unavailable,thread_time_ns=unavailable),
                _trace=lambda stage,*args,**fields:records.append((stage,fields)),
                touch_buffer=lambda:touches.append(1) or 19)
            connection=FIXTURE.Connection(reply=b'touch\n');ns['handle'](connection)
            self.assertEqual(touches,[1]);self.assertIn(b'checksum=19',connection.calls[1][1])
            self.assertEqual(dict(records)['touch_end'],{'body_mono_ns':[None,None],'body_thread_ns':[None,None],'body_timing':'unknown'})

    def test_original_touch_exception_is_not_replaced_or_given_fake_end(self):
        with tempfile.TemporaryDirectory() as directory:
            ns=self.server(Path(directory));events=[];primary=ValueError('original touch error')
            def unavailable():raise OSError('diagnostic clock unavailable')
            def touch():events.append('touch');raise primary
            ns.update(time=types.SimpleNamespace(monotonic_ns=unavailable,thread_time_ns=unavailable),
                _trace=lambda stage,*args,**fields:events.append(stage),touch_buffer=touch)
            with self.assertRaises(ValueError) as caught:ns['handle'](FIXTURE.Connection(reply=b'touch\n'))
            self.assertIs(caught.exception,primary);self.assertEqual(events.count('touch'),1)
            self.assertNotIn('touch_end',events);self.assertEqual(events[-2:],['exception','exit'])

    def test_unchanged_page_loop_and_no_new_request_operations(self):
        def touch_ast(code):return ast.dump(next(n for n in ast.parse(code).body if isinstance(n,ast.FunctionDef) and n.name=='touch_buffer'))
        self.assertEqual(touch_ast(D.MEM_SERVER_CODE),touch_ast(D.diagnostic_mem_server_code()))
        tree=ast.parse(D.diagnostic_mem_server_code());handler=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='handle')
        calls=[ast.unparse(n.func) for n in ast.walk(handler) if isinstance(n,ast.Call)]
        self.assertEqual(calls.count('touch_buffer'),1);self.assertEqual(calls.count('conn.recv'),1);self.assertEqual(calls.count('conn.sendall'),1)
        self.assertNotIn('time.sleep',calls)

    def test_body_record_fits_512_with_conservative_64bit_clock_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'trace';ns={'os':os,'_trace_path':str(path),'_trace_token':'e2b-1790794300-01234567-n64'}
            exec(D._TEMPORAL_TRACE_HELPER,ns)
            large=9223372036854775807
            ns['time']=types.SimpleNamespace(time_ns=lambda:large,monotonic_ns=lambda:large)
            ns['_trace']('touch_end','verify',{'local':('127.0.0.1',38765),'peer':('127.0.0.1',65535)},
                body_mono_ns=[large-100,large],body_thread_ns=[large-20,large],body_timing='captured')
            data=path.read_bytes();record=json.loads(data)
            self.assertLessEqual(len(data),512);self.assertEqual(record['stage'],'touch_end')
            self.assertEqual(record['body_mono_ns'],[large-100,large]);self.assertEqual(record['body_thread_ns'],[large-20,large])


if __name__=='__main__':unittest.main()
