"""Exercise the actual upload helper without a running Cube service."""
import ast
import hashlib
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
