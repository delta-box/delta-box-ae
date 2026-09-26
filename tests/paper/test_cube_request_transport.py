"""Exercise the actual upload helper without a running Cube service."""
import argparse
import ast
import shutil
import statistics
import uuid
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / 'ae/vendor/finalbench/cube_cow_peagle_mcts30_2x_numa12_realrtt/scripts/cube_cow_schedule_replay.py'


class CubeRequestTransport(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(DRIVER.read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'write_action_request']
        self.reconnect = Mock()
        self.ns = dict(Sandbox=object, Any=object, hashlib=hashlib,
                       is_retryable_connection_error=lambda e: 'connection reset' in str(e),
                       reconnect_sandbox=self.reconnect)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(DRIVER), 'exec'), self.ns)
        self.upload = self.ns['write_action_request']

    def sandbox(self, error=None, text='{"seq":4}'):
        return SimpleNamespace(files=SimpleNamespace(write=Mock(side_effect=error), read=Mock(return_value=text)))

    def test_504_reconnects_and_verifies_exact_request_before_execution(self):
        old = self.sandbox(OSError('<h1>504 Gateway Time-out</h1>'))
        fresh = self.sandbox()
        self.reconnect.return_value = fresh
        sb, record = self.upload(old, '{"seq":4}')
        self.assertIs(sb, fresh)
        self.assertEqual(record['attempts'], 2)
        self.assertEqual(len(record['retry_errors']), 1)
        fresh.files.write.assert_called_once_with('/tmp/action.req.json', '{"seq":4}')
        self.assertEqual(record['verified_sha256'], hashlib.sha256(b'{"seq":4}').hexdigest())

    def test_corrupt_upload_fails_without_retry(self):
        with self.assertRaisesRegex(ValueError, 'read-back'):
            self.upload(self.sandbox(text='truncated'), '{"seq":4}')
        self.reconnect.assert_not_called()

    def test_permission_error_is_not_retried(self):
        with self.assertRaises(PermissionError):
            self.upload(self.sandbox(PermissionError('denied')), '{"seq":4}')
        self.reconnect.assert_not_called()

    def test_retries_are_bounded(self):
        sb = self.sandbox(OSError('connection reset'))
        self.reconnect.return_value = sb
        with self.assertRaises(OSError):
            self.upload(sb, '{"seq":4}')
        self.assertEqual(sb.files.write.call_count, 3)
        self.assertEqual(self.reconnect.call_count, 2)

class CubeRequestFraming(unittest.TestCase):
    def test_real_worker_reads_framed_requests_without_writer_eof(self):
        tree = ast.parse(DRIVER.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'encode_action_request')
        ns = dict(json=json, Any=object)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(DRIVER), 'exec'), ns)
        worker = DRIVER.parent.parent / 'e2b_slim_action_worker.py'
        child = """
import runpy, sys, types
from pathlib import Path
runner = types.ModuleType('e2b_slim_action_runner_worker_ops')
calls = []
def run(request):
    calls.append(request['seq'])
    return {'ok': True, 'seq': request['seq'], 'calls': list(calls)}
runner.run = run
sys.modules[runner.__name__] = runner
worker, directory = sys.argv[1:]
root = Path(directory)
ns = runpy.run_path(worker)
ns['serve_fifo'](root/'input', root/'response', root/'ready')
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = subprocess.Popen([sys.executable, '-c', child, str(worker), directory],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            fd = None
            def await_file(path):
                deadline = time.monotonic() + 5
                while not path.exists() and time.monotonic() < deadline:
                    if proc.poll() is not None:
                        self.fail(proc.stderr.read().decode())
                    time.sleep(.01)
                self.assertTrue(path.exists(), str(path))
            try:
                await_file(root/'ready')
                fd = os.open(root/'input', os.O_RDWR | os.O_NONBLOCK)
                # Legacy JSON waits for EOF while any writer stays open.
                os.write(fd, b'{"seq":1}')
                time.sleep(.15)
                self.assertFalse((root/'response').exists())
                os.write(fd, b'\n')
                await_file(root/'response')
                self.assertEqual(json.loads((root/'response').read_text())['calls'], [1])
                (root/'response').unlink()
                # The production encoder provides its own frame boundary.
                request = {'seq': 2, 'text': 'one\ntwo'}
                framed = ns['encode_action_request'](request)
                self.assertEqual(json.loads(framed), request)
                os.write(fd, framed.encode())
                await_file(root/'response')
                self.assertEqual(json.loads((root/'response').read_text()),
                                 {'ok': True, 'seq': 2, 'calls': [1, 2]})
                self.assertIsNone(proc.poll())
            finally:
                if fd is not None:
                    os.close(fd)
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()


class CubeActionFailure(unittest.TestCase):
    def load(self, names, **bindings):
        nodes = [node for node in ast.parse(DRIVER.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
        ns = dict(argparse=argparse, Path=Path, Any=object, Sandbox=object,
                  json=json, os=os, sys=sys, time=time, shutil=shutil, uuid=uuid,
                  statistics=statistics, subprocess=subprocess)
        ns.update(bindings)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(DRIVER), 'exec'), ns)
        return ns

    def test_failed_or_uncertain_action_never_reexecutes_or_snapshots(self):
        for command_ok in (False, True):
            with self.subTest(command_ok=command_ok), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                schedule = root/'schedule.json'
                schedule.write_text('[]')
                sb = SimpleNamespace(sandbox_id='test-sandbox', kill=Mock(),
                                     files=SimpleNamespace(read=Mock(return_value='{"ok":false,"error":"worker failed"}')))
                action = dict(ok=command_ok, stderr='connection reset after dispatch',
                              stdout='', elapsed_ms=300000)
                dispatch = Mock(side_effect=[action, dict(ok=True, stdout='worker log')])
                snapshot = Mock(side_effect=AssertionError('snapshot must not run'))
                retry = Mock(side_effect=AssertionError('uncertain action must not retry'))
                probe = Mock(return_value={'probe_error': 'diagnostic timed out'})
                ns = self.load(
                    {'run_instance', 'encode_action_request'},
                    BASE=root, Sandbox=SimpleNamespace(create=Mock(return_value=sb)),
                    load_schedule=lambda p: [dict(type='ckpt', node_id=1, ckpt_id='c1',
                        worker_ops=[dict(action_step_idx=0)])],
                    build_recorded_action_outputs=lambda instance: {},
                    prepare_sandbox=Mock(return_value={}),
                    make_schedule_action_request=lambda **kw: {'seq': kw['seq']},
                    write_action_request=lambda sandbox, text: (sandbox, {'verified': True}),
                    cube_run=dispatch, cube_run_retry=retry,
                    schedule_action_command=lambda *args: 'dispatch-action',
                    snapshot_create=snapshot, capture_action_failure=probe,
                    tail_record=lambda x: x, sha256_file=lambda path: 'source',
                    write_json=lambda path, data: path.write_text(json.dumps(data)),
                )
                args = SimpleNamespace(run_id_prefix='test', max_events=0, template='t',
                    sandbox_timeout=10, warm_action_worker=True, materialize_file_context=False,
                    upload_chunk_mb=1, no_llm_sleep=True, worker_timeout=300, delete_snapshots=False,
                    template_cpu_millicores=1000, template_memory_mb=8192,
                    writable_layer_size=1, host_cpus='28-31')
                result = ns['run_instance'](args, {'instance':'test', 'schedule':str(schedule)})
                self.assertFalse(result['ok'])
                self.assertEqual(result['status'], 'fail')
                self.assertEqual(result['n_ckpt_events'], 0)
                failed = result['iterations'][0]
                self.assertTrue(failed['checkpoint_not_started'])
                self.assertNotIn('checkpoint_wall_ms', failed)
                self.assertEqual(failed['cube_steps'][0]['stderr'], action['stderr'])
                self.assertEqual([call.args[1] for call in dispatch.call_args_list],
                                 ['dispatch-action', 'cat /tmp/finalbench/action_worker.log 2>/dev/null || true'])
                probe.assert_called_once()
                snapshot.assert_not_called()
                retry.assert_not_called()
                sb.kill.assert_called_once()

    def test_probe_timeout_keeps_original_failure_and_partial_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def timeout(command, **kw):
                self.assertEqual(kw['timeout'], 20)
                self.assertFalse(kw['check'])
                Path(command[-1]).write_text('{"status":"running","steps":["process"]}')
                raise subprocess.TimeoutExpired(command, 20)
            fake = SimpleNamespace(run=Mock(side_effect=timeout), TimeoutExpired=subprocess.TimeoutExpired)
            ns = self.load({'capture_action_failure'}, BASE=root, CUBE_SDK=root,
                           write_json=lambda p, data: p.write_text(json.dumps(data)))
            ns['subprocess'] = fake
            record = ns['capture_action_failure'](SimpleNamespace(sandbox_id='s'), root, 55,
                                                 '{"seq":55}\n', {'ok':False,'stderr':'original'}, None)
            saved = root/'action-failures/event-0055'
            self.assertIn('20 seconds', record['probe_error'])
            self.assertEqual(json.loads((saved/'failure.json').read_text())['action']['stderr'], 'original')
            self.assertEqual(json.loads((saved/'probe.json').read_text())['steps'], ['process'])
