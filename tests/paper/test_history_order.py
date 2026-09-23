"""Producer-side compatibility and optional strict replay validation."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from replay import history_order


ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = ROOT / 'ae/vendor/spr_payload'
EVIDENCE = ROOT / 'ae/report/first-round-20260921/raw/first-round-cpu-20260921/suite'


def recorded_trace(messages):
    return {'root': {'node_id': 3, 'completions': {'build_action': {
        'input': messages, 'response': {'created': 1, 'choices': [
            {'message': {'role': 'assistant', 'content': 'recorded response'}}]},
    }}, 'children': []}}


def evidence():
    return [json.loads(path.read_text()) for path in sorted(EVIDENCE.glob(
        'table-02-*/diagnostics/mock_mismatch_*.json'))]


def render_live_history(messages):
    """Exercise the producer call inserted into the synthetic CodeSpan branch."""
    result = deepcopy(messages)
    for message in result:
        content = message.get('content', '')
        if message.get('role') != 'assistant' or not content.startswith(history_order.SYNTHETIC_PREFIX):
            continue
        args = json.loads(content[len(history_order.SYNTHETIC_PREFIX):])
        for file in args['files']:
            file['span_ids'] = history_order.restore_span_order(file['file_path'], file['span_ids'])
        message['content'] = history_order.SYNTHETIC_PREFIX + json.dumps(args, indent=2, ensure_ascii=False)
    return result


class HistoryOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(PAYLOAD))
        try:
            spec = importlib.util.spec_from_file_location('strict_history_test_mock', PAYLOAD / 'mock_llm_server.py')
            cls.strict = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.strict)
        finally:
            sys.path.pop(0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)

    def table(self, messages):
        trace = self.work / 'trajectory.json'
        trace.write_text(json.dumps(recorded_trace(messages)))
        table = history_order.build_order_table(trace)
        orders = {history_order._signature(row['file_path'], row['span_ids']): row['span_ids']
                  for row in table['orders']}
        return orders, 'test-table-sha256', table['trace_sha256']

    def strict_accepts(self, messages, recorded):
        state = self.strict.ServerState(self.work, message_policy='strict')
        state.instance_id = 'test-instance'
        state.sequence = [SimpleNamespace(input=recorded,
            input_hash=self.strict.canonical_messages_hash(recorded),
            purpose='build_action', node_id=3, response={'recorded': True}, dur_s=0)]
        handler = self.strict.MockHandler.__new__(self.strict.MockHandler)
        handler.server = SimpleNamespace(state=state)
        handler._read_body = lambda: json.dumps({'messages': messages}).encode()
        handler._send_json = mock.Mock()
        handler.close_connection = False
        with mock.patch.dict('os.environ', {'MOCK_MISMATCH_DIR': str(self.work)}), \
                mock.patch.object(self.strict.log, 'error'):
            handler._handle_chat_completions()
        return state.n_served == 1 and state.n_mismatch == 0

    def test_two_real_failures_rebuild_at_producer_and_pass_original_mock(self):
        cases = evidence()
        self.assertEqual(len(cases), 2)
        for case in cases:
            with self.subTest(cursor=case['cursor']):
                request, recorded = case['request_messages'], case['expected_messages']
                original = deepcopy(request)
                self.assertFalse(self.strict_accepts(request, recorded))
                with mock.patch.object(history_order, '_runtime_table', return_value=self.table(recorded)):
                    with mock.patch.object(history_order, '_counts', dict(lookups=0, reordered=0, misses=0)) as counts:
                        rebuilt = render_live_history(request)
                        self.assertGreater(counts['reordered'], 0)
                self.assertEqual(rebuilt, recorded)
                self.assertEqual(request, original)
                self.assertTrue(self.strict_accepts(rebuilt, recorded))

    def test_criu_post_restore_mismatch_is_only_synthetic_span_order(self):
        case = json.loads((ROOT / 'ae/report/replay-fixes-20260921/criu-host/'
                           'mock_mismatch_astropy__astropy-13033_c3.json').read_text())
        request, recorded = case['request_messages'], case['expected_messages']
        self.assertFalse(self.strict_accepts(request, recorded))
        with mock.patch.object(history_order, '_runtime_table', return_value=self.table(recorded)):
            rebuilt = render_live_history(request)
        self.assertEqual(rebuilt, recorded)
        self.assertTrue(self.strict_accepts(rebuilt, recorded))
        changed = deepcopy(rebuilt)
        changed[5]['content'] += '\nchanged restored observation'
        self.assertFalse(self.strict_accepts(changed, recorded))

    def test_changed_span_path_and_multiplicity_fail_before_request(self):
        case = evidence()[0]
        request, recorded = case['request_messages'], case['expected_messages']
        file = json.loads(request[4]['content'][len(history_order.SYNTHETIC_PREFIX):])['files'][0]
        bad = [(file['file_path'] + '.wrong', file['span_ids']),
               (file['file_path'], ['wrong'] + file['span_ids'][1:]),
               (file['file_path'], file['span_ids'][:-1]),
               (file['file_path'], file['span_ids'] + [file['span_ids'][0]])]
        with mock.patch.object(history_order, '_runtime_table', return_value=self.table(recorded)):
            for path, spans in bad:
                with self.subTest(path=path, spans=spans), \
                        mock.patch.dict('os.environ', {'MOCK_MESSAGE_POLICY': 'strict'}), \
                        self.assertRaisesRegex(ValueError, 'unrecorded'):
                    history_order.restore_span_order(path, spans)

    def test_observation_code_action_line_and_message_order_still_fail_strict_mock(self):
        case = evidence()[0]
        request, recorded = case['request_messages'], case['expected_messages']
        with mock.patch.object(history_order, '_runtime_table', return_value=self.table(recorded)):
            rebuilt = render_live_history(request)
        mutations = []
        changed = deepcopy(rebuilt)
        changed[5]['content'] += '\nchanged code or observation'
        mutations.append(changed)
        changed = deepcopy(rebuilt)
        changed[2]['content'] += '\nchanged actual action'
        mutations.append(changed)
        changed = deepcopy(rebuilt)
        changed[4]['content'] = changed[4]['content'].replace('"start_line": null', '"start_line": 17')
        mutations.append(changed)
        changed = deepcopy(rebuilt)
        changed[2], changed[4] = changed[4], changed[2]
        mutations.append(changed)
        changed = deepcopy(rebuilt)
        changed[4]['content'] += ' '
        mutations.append(changed)
        for changed in mutations:
            self.assertFalse(self.strict_accepts(changed, recorded))

    def test_ambiguous_recorded_order_is_rejected(self):
        case = evidence()[0]
        first = recorded_trace(case['expected_messages'])
        first['root']['completions']['second'] = {'input': case['request_messages']}
        path = self.work / 'ambiguous.json'
        path.write_text(json.dumps(first))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            history_order.build_order_table(path)
        audit = history_order.build_order_table(path, message_policy='audit')
        self.assertGreater(audit['ambiguous_keys_passthrough'], 0)

    def test_actual_viewcode_action_does_not_enter_order_table(self):
        messages = deepcopy(evidence()[0]['expected_messages'])
        messages[4]['content'] = messages[4]['content'].replace(
            "Let's view the content in the updated files", 'Inspect the failing function')
        self.assertEqual(self.table(messages)[0], {})

    def test_audit_unknown_spans_preserve_live_content_without_logging(self):
        spans = ['new-span', 'new-span']
        with mock.patch.object(history_order, '_runtime_table', return_value=({}, 'table', 'trace')), \
                mock.patch.dict('os.environ', {'MOCK_MESSAGE_POLICY': 'audit'}), \
                mock.patch('builtins.print', side_effect=AssertionError('hot-path print')), \
                mock.patch.object(history_order.json, 'dumps', side_effect=AssertionError('hot-path serialization')):
            self.assertEqual(history_order.restore_span_order('new.py', spans), spans)

    def test_staging_is_private_source_locked_and_patches_only_synthetic_call(self):
        source = self.work / 'shared/moatless'
        source.mkdir(parents=True)
        # A namespace-only fixture loses to any later regular package, including
        # Moatless installed in the Linux AE venv. Match the real package layout.
        (source / '__init__.py').write_text('')
        installed = self.work / 'installed/moatless'
        installed.mkdir(parents=True)
        (installed / '__init__.py').write_text('')
        (installed / 'message_history.py').write_text('raise AssertionError("wrong installed Moatless imported")\n')
        context = 'class ContextFile:\n    span_ids = ["imports", "function"]\n'
        original = (
            'class BaseModel: pass\n'
            'def CodeSpan(**kwargs): return kwargs\n'
            'class MessageHistoryGenerator(BaseModel):\n'
            '    def synthesize(self, file_path, context_file):\n'
            '        return CodeSpan(\n'
            '                                        file_path=file_path,\n'
            + history_order._CALL + '\n'
            '        )\n'
            '    def actual_action(self, args):\n'
            '        return args\n'
        )
        (source / 'file_context.py').write_text(context)
        (source / 'message_history.py').write_text(original)
        payload = self.work / 'payload'
        payload.mkdir()
        for name in ('mock_llm_server.py', 'protocol.py', 'replay_driver.py', 'trajectory_index.py'):
            shutil.copy2(PAYLOAD / name, payload / name)
        (payload / 'moatless-det-src').symlink_to(source.parent, target_is_directory=True)
        trace = self.work / 'trace.json'
        trace.write_text(json.dumps(recorded_trace(evidence()[0]['expected_messages'])))
        with self.assertRaisesRegex(ValueError, 'source lock mismatch'):
            history_order.stage_history_order(payload, trace)
        hashes = {name: history_order._sha256(source / name) for name in history_order.PINNED_SOURCES}
        with mock.patch.object(history_order, 'PINNED_SOURCES', hashes):
            record = history_order.stage_history_order(payload, trace)
        self.assertEqual((source / 'message_history.py').read_text(), original)
        self.assertEqual((source / 'file_context.py').read_text(), context)
        self.assertFalse((payload / 'moatless-det-src').is_symlink())
        staged = payload / 'moatless-det-src/moatless'
        patched = (staged / 'message_history.py').read_text()
        compile(patched, 'message_history.py', 'exec')
        self.assertIn('span_ids=_restore_span_order(file_path, context_file.span_ids)', patched)
        self.assertIn('def actual_action(self, args):\n        return args', patched)
        case = evidence()[0]
        requested_file = json.loads(case['request_messages'][4]['content'][len(history_order.SYNTHETIC_PREFIX):])['files'][0]
        code = '''
import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[3])
sys.path.insert(0, sys.argv[1])
import moatless.message_history as history
assert Path(history.__file__).resolve() == Path(sys.argv[1]).resolve() / 'moatless/message_history.py'
from moatless.message_history import MessageHistoryGenerator
file = json.loads(sys.argv[2])
gen = MessageHistoryGenerator()
print(json.dumps(gen.synthesize(file['file_path'], SimpleNamespace(span_ids=file['span_ids']))))
assert gen.actual_action(file) == file
'''
        completed = subprocess.run([sys.executable, '-c', code, str(staged.parent), json.dumps(requested_file), str(installed.parent)],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        generated = json.loads(completed.stdout)
        expected_file = json.loads(case['expected_messages'][4]['content'][len(history_order.SYNTHETIC_PREFIX):])['files'][0]
        self.assertEqual(generated['span_ids'], expected_file['span_ids'])
        self.assertEqual(record['upstream_sha256'], hashes)
        for name, digest in record['staged_sha256'].items():
            self.assertEqual(digest, history_order._sha256(staged / name))
        for name, digest in record['mock_source_sha256'].items():
            self.assertEqual((payload / name).read_bytes(), (PAYLOAD / name).read_bytes())
            self.assertEqual(digest, history_order._sha256(PAYLOAD / name))

if __name__ == '__main__':
    unittest.main()
