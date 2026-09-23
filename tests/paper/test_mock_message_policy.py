"""Fixed-cursor audit behavior and post-measurement diagnostics contract."""
from contextlib import ExitStack
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = ROOT / 'ae/vendor/spr_payload'
sys.path.insert(0, str(PAYLOAD))
try:
    spec = importlib.util.spec_from_file_location('audit_test_mock_llm', PAYLOAD / 'mock_llm_server.py')
    server = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = server
    spec.loader.exec_module(server)
finally:
    sys.path.pop(0)


def completion(messages, number=0):
    return SimpleNamespace(input=messages,
        input_hash=server.canonical_messages_hash(messages), purpose='build_action',
        node_id=number, response={'id': f'recorded-{number}'}, dur_s=0)


class MockMessagePolicyTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.messages = [{'role': 'user', 'content': 'recorded text'}]

    def state(self, policy='audit', **kwargs):
        state = server.ServerState(self.root, message_policy=policy, **kwargs)
        state.instance_id = 'test-instance'
        state.sequence = [completion(self.messages)]
        return state

    def request(self, state, messages=None, *, body=None):
        handler = server.MockHandler.__new__(server.MockHandler)
        handler.server = SimpleNamespace(state=state)
        handler._read_body = lambda: body if body is not None else json.dumps({'messages': messages}).encode()
        handler._send_json = mock.Mock()
        handler.close_connection = False
        handler._handle_chat_completions()
        return handler

    def test_audit_default_and_explicit_strict_configuration(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            self.assertEqual(server.ServerState(self.root).message_policy, 'audit')
        with mock.patch.dict('os.environ', {'MOCK_MESSAGE_POLICY': 'strict'}):
            self.assertEqual(server.ServerState(self.root).message_policy, 'strict')
            self.assertEqual(server.ServerState(self.root, message_policy='audit').message_policy, 'audit')
        with self.assertRaises(ValueError):
            self.state('ignored')
        with self.assertRaises(ValueError):
            self.state(audit_max_bytes=-1)

    def test_audit_uses_fixed_cursor_even_when_request_matches_later_response(self):
        state = self.state()
        other = [{'role': 'user', 'content': 'later text'}]
        state.sequence.append(completion(other, 1))
        response = self.request(state, other)
        response._send_json.assert_called_once_with(200, {'id': 'recorded-0'})
        self.assertFalse(response.close_connection)
        self.assertEqual((state.cursor, state.n_served, state.n_mismatch, state.n_protocol_errors), (1, 1, 1, 0))
        report = state.flush_audit()
        self.assertEqual(report['records'][0]['cursor'], 0)
        self.assertEqual(report['records'][0]['request_hash'], server.canonical_messages_hash(other))
        self.assertEqual(report['records'][0]['differences'], [{
            'index': 0, 'kind': 'changed_message', 'request': other[0], 'expected': self.messages[0]}])
        self.assertEqual(report['stats']['audit_events_pending'], 0)
        self.assertEqual(report['stats']['n_mismatch'], 1)
        second = state.flush_audit()
        self.assertEqual(second['records'], [])
        self.assertEqual(second['flush_id'], 2)

    def test_strict_mismatch_closes_without_advancing_then_exact_request_succeeds(self):
        state = self.state('strict')
        handler = self.request(state, [{'role': 'user', 'content': 'changed'}])
        self.assertTrue(handler.close_connection)
        handler._send_json.assert_not_called()
        self.assertEqual((state.cursor, state.n_served, state.n_mismatch), (0, 0, 1))
        handler = self.request(state, self.messages)
        handler._send_json.assert_called_once_with(200, {'id': 'recorded-0'})
        self.assertEqual(state.cursor, 1)

    def test_request_does_not_format_hash_diff_or_diagnostic_output(self):
        state = self.state()
        body = json.dumps({'messages': [{'role': 'user', 'content': 'changed'}]}).encode()
        with ExitStack() as stack:
            for target in ('builtins.open', 'builtins.print'):
                stack.enter_context(mock.patch(target, side_effect=AssertionError('diagnostic I/O during request')))
            for target, name in ((server, 'canonical_messages_hash'), (server, '_message_differences'),
                                 (server.json, 'dumps'), (server.log, 'info'), (server.log, 'error')):
                stack.enter_context(mock.patch.object(target, name, side_effect=AssertionError('formatting during request')))
            handler = self.request(state, body=body)
            handler.log_message(mock.Mock(), object())
        self.assertEqual(state.n_served, 1)
        self.assertEqual(state.stats()['audit_payload_bytes'], len(body))
        # Heavy diagnostic work is reached only after that guarded interval.
        with mock.patch.object(server, '_message_differences', wraps=server._message_differences) as diff:
            self.assertEqual(len(state.flush_audit()['records']), 1)
        diff.assert_called_once()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_protocol_errors_are_fatal_under_both_policies(self):
        for policy in ('audit', 'strict'):
            for body in (b'invalid', b'[]', b'{}', b'{"messages":null}', b'{"messages":[7]}'):
                with self.subTest(policy=policy, body=body):
                    state = self.state(policy)
                    handler = self.request(state, body=body)
                    self.assertTrue(handler.close_connection)
                    handler._send_json.assert_not_called()
                    self.assertEqual((state.cursor, state.n_mismatch, state.n_protocol_errors), (0, 0, 1))
                    self.assertEqual(state.flush_audit()['records'][0]['protocol_error'], 'malformed_chat_request')
            for condition in ('not_loaded', 'overrun', 'negative_cursor'):
                state = self.state(policy)
                if condition == 'not_loaded':
                    state.sequence = []
                else:
                    state.cursor = 1 if condition == 'overrun' else -1
                before = state.cursor
                handler = self.request(state, self.messages)
                handler._send_json.assert_not_called()
                self.assertTrue(handler.close_connection)
                self.assertEqual((state.cursor, state.n_served, state.n_mismatch, state.n_protocol_errors),
                                 (before, 0, 0, 1))

    def test_byte_and_record_caps_are_independent_and_omissions_are_explicit(self):
        body = json.dumps({'messages': [{'role': 'user', 'content': 'different'}]}).encode()
        state = self.state(audit_max_records=2, audit_max_bytes=len(body))
        for _ in range(3):
            self.request(state, body=body)
            state.rewind(0)
        stats = state.stats()
        self.assertEqual((stats['n_mismatch'], stats['audit_records_pending'], stats['audit_payload_bytes']), (3, 2, len(body)))
        self.assertEqual((stats['audit_records_dropped'], stats['audit_payloads_omitted']), (1, 1))
        for change in (lambda: state.load('other', 'ms'), state.reset):
            with self.assertRaisesRegex(ValueError, 'unflushed audit events'):
                change()
        report = state.flush_audit()
        self.assertEqual(report['buffer']['events_since_previous_flush'], 3)
        self.assertEqual(report['buffer']['records_dropped_since_previous_flush'], 1)
        self.assertEqual(report['buffer']['payloads_omitted_since_previous_flush'], 1)
        self.assertTrue(report['records'][1]['request_payload_omitted'])
        self.assertNotIn('request_hash', report['records'][1])
        self.assertEqual(state.reset(), {'ok': True})

    def test_dropped_only_protocol_event_must_be_flushed_before_reset_or_load(self):
        state = self.state(audit_max_records=0, audit_max_bytes=0)
        self.request(state, body=b'bad json')
        self.assertEqual(state.stats()['audit_records_pending'], 0)
        self.assertEqual(state.stats()['audit_events_pending'], 1)
        with self.assertRaisesRegex(ValueError, 'unflushed audit'):
            state.reset()
        with self.assertRaisesRegex(ValueError, 'unflushed audit'):
            state.load('another', 'ms')
        report = state.flush_audit()
        self.assertEqual(report['records'], [])
        self.assertEqual(report['stats']['n_protocol_errors'], 1)
        self.assertEqual(report['buffer']['records_dropped_since_previous_flush'], 1)
        state.reset()

    def test_bad_admin_rewind_is_counted_and_never_reports_ok(self):
        for cursor in (-1, 2, True, 0.5, '0'):
            with self.subTest(cursor=cursor):
                state = self.state()
                handler = server.MockHandler.__new__(server.MockHandler)
                handler.server = SimpleNamespace(state=state)
                handler._read_body = lambda: json.dumps({'cursor': cursor}).encode()
                handler._send_json = mock.Mock()
                handler._handle_admin_rewind()
                code, payload = handler._send_json.call_args.args
                self.assertEqual(code, 400)
                self.assertFalse(payload['ok'])
                self.assertEqual((state.cursor, state.n_protocol_errors), (0, 1))
                self.assertEqual(state.stats()['audit_events_pending'], 1)

    def test_exact_match_has_no_pending_audit_and_reset_needs_no_flush(self):
        state = self.state()
        self.request(state, self.messages)
        self.assertEqual(state.stats()['audit_events_pending'], 0)
        self.assertEqual(state.stats()['n_mismatch'], 0)
        self.assertEqual(state.reset(), {'ok': True})

    def test_json_comparison_matches_canonical_hash_semantics(self):
        values = [None, False, True, 0, 1, 0.0, -0.0, 1.0, float('nan'), float('inf'),
                  float('-inf'), '', '0', 'é雪', [], [1], [1.0], {}, {'a': 1},
                  {'a': 1, 'b': [False, {'x': -0.0}]}, {'b': [False, {'x': -0.0}], 'a': 1}]
        for left in values:
            for right in values:
                with self.subTest(left=left, right=right):
                    actual = [{'role': 'user', 'content': left}]
                    expected = [{'content': right, 'role': 'user'}]
                    self.assertEqual(server._json_equal(actual, expected),
                                     server.canonical_messages_hash(actual) == server.canonical_messages_hash(expected))

    def test_real_c14_missing_history_is_exported_without_stopping_audit(self):
        case = json.loads((ROOT / 'ae/report/replay-fixes-20260921/criu-host/'
                           'mock_mismatch_astropy__astropy-13033_c14.json').read_text())
        state = self.state()
        state.sequence = [completion(case['expected_messages'])]
        handler = self.request(state, case['request_messages'])
        handler._send_json.assert_called_once_with(200, {'id': 'recorded-0'})
        report = state.flush_audit()
        self.assertEqual([row['index'] for row in report['records'][0]['differences']], [18, 19])
        self.assertTrue(all(row['kind'] == 'missing_request_message' for row in report['records'][0]['differences']))

    def test_actual_http_flush_after_completion_exports_and_drains(self):
        state = self.state()
        listener = server.TCPHTTPServer('127.0.0.1', 0, state)
        thread = threading.Thread(target=listener.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            with mock.patch.object(server.log, 'info') as access_log:
                conn = http.client.HTTPConnection(*listener.server_address, timeout=3)
                conn.request('POST', '/v1/chat/completions', json.dumps({'messages': []}))
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {'id': 'recorded-0'})
                conn.close()
                conn = http.client.HTTPConnection(*listener.server_address, timeout=3)
                conn.request('POST', '/admin/audit/flush', '{}')
                response = conn.getresponse()
                exported = json.loads(response.read())
                conn.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(exported['message_policy'], 'audit')
                self.assertEqual(exported['stats']['n_mismatch'], 1)
                self.assertEqual(exported['stats']['n_protocol_errors'], 0)
                self.assertEqual(exported['stats']['audit_events_pending'], 0)
                self.assertEqual(len(exported['records']), 1)
                access_log.assert_not_called()
        finally:
            listener.shutdown()
            thread.join(timeout=3)
            listener.server_close()


if __name__ == '__main__':
    unittest.main()
