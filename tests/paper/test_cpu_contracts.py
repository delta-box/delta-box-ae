"""Regression checks for incomplete replay, timing attribution and cleanup."""
import csv
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'ae'), str(ROOT / 'ae/runners'), str(ROOT / 'ae/runners/deltabox')]
import baseline
from legacy_schedule import convert_legacy
from repro.common import configured_value, load_config
from repro.process import execute


class CPUContractTests(unittest.TestCase):
    def test_environment_values_are_expanded_but_missing_backend_is_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            config.write_text('{"e2b":{"from_build":"${AE_TEST_BUILD}"},"unused":"${AE_MISSING}"}')
            with patch.dict(os.environ, {'AE_TEST_BUILD': 'actual-build'}):
                data = load_config(config)
            self.assertEqual(configured_value(data, 'e2b.from_build'), 'actual-build')
            with self.assertRaises(ValueError):
                configured_value(data, 'unused')

    def test_e2b_rejects_truncated_and_wrong_node_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace, result = root / 'trace.json', root / 'pilot.json'
            action = {'action': {'action_args_class': 'a.ViewCodeArgs'}}
            trace.write_text(json.dumps({'root': {'node_id': 0, 'children': [
                {'node_id': 1, 'action_steps': [action], 'children': []},
                {'node_id': 2, 'action_steps': [action], 'children': []}]}}))
            events = [{'event': 'expand', 'new_node': n, 'parent': 0} for n in (1, 2)]
            data = {'iterations': [{'ok': True, 'node_id': n, 'event': {'new_node_id': n,
                'selected_node_id': 0, 'n_worker_actions': 1,
                'action_events': [{'node_id': n, 'action_args_class': 'a.ViewCodeArgs'}]},
                'e2b_steps': [{'ok': True}]} for n in (1, 2)],
                'n_e2b_steps': 2, 'mock_stats': {'n_mismatch': 0}}
            sys.path.insert(0, str(ROOT / 'ae/vendor/finalbench/replay_copytree'))
            with patch('walker.parse_trajectory', return_value={'events': events}):
                result.write_text(json.dumps(data))
                baseline.validate_trace_events(result, 'e2b', trace, None)
                data['iterations'].pop()
                result.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, 'incomplete'):
                    baseline.validate_trace_events(result, 'e2b', trace, None)
                data['iterations'][0]['event']['selected_node_id'] = 9
                result.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, 'target'):
                    baseline.validate_trace_events(result, 'e2b', trace, 1)

    def test_replay_zero_llm_attribution_cannot_be_inconsistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pilot = root / 'summary.json'
            pilot.write_text(json.dumps({'ok': True, 'requested_restores': 1, 'completed_restores': 1}))
            row = dict(ok='True', rc='0', restore_ms=12, mock_sleep_ms=7, restore_zero_llm_ms=5, mock_mismatch=0)
            def write():
                with (root / 'restores.csv').open('w') as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(row)); writer.writeheader(); writer.writerow(row)
            write()
            self.assertEqual(baseline.validate_pilot(pilot, 'replay')['restores'], 1)
            row['restore_zero_llm_ms'] = 6; write()
            with self.assertRaisesRegex(ValueError, 'inconsistent'):
                baseline.validate_pilot(pilot, 'replay')

    def test_legacy_bootstrap_and_parent_restore_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            states = [{'id': i, 'name': name, 'previous_state_id': parent, 'created_at': f'2026-01-01T00:00:0{i}',
                       'snapshot': {}, 'actions': []} for i, name, parent in ((1, 'SearchCode', None), (2, 'EditCode', 1), (3, 'SearchCode', 1))]
            (root / 'trajectory.json').write_text(json.dumps({'workspace': {'repository': {'commit': 'a' * 40}}, 'transitions': states}))
            meta = convert_legacy(root, 'test__repo-1', root / 'schedule.jsonl', adaptive=True)
            events = [json.loads(line) for line in (root / 'schedule.jsonl').read_text().splitlines()]
            self.assertEqual(meta['legacy_checkpoint_count'], 3)
            self.assertTrue(events[0]['bootstrap'])
            self.assertEqual(events[1]['strategy'], 'lightweight')
            self.assertEqual(events[2]['strategy'], 'standard')
            self.assertEqual(events[3]['restore_to_ckpt_id'], events[1]['ckpt_id'])

    def test_timeout_cleans_nested_process_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / 'child.py'
            child.write_text('import os,time\nfrom pathlib import Path\nPath("child.pid").write_text(str(os.getpid()))\ntime.sleep(60)\n')
            wrapper = root / 'nested.py'
            wrapper.write_text(f'import sys\nfrom pathlib import Path\nsys.path.insert(0,{str(ROOT / "ae")!r})\n'
                               'from repro.process import execute\nexecute([sys.executable,"child.py"],Path("nested"),cwd=Path.cwd(),timeout=60)\n')
            result = execute([sys.executable, str(wrapper)], root / 'outer', cwd=root, timeout=1)
            self.assertEqual(result['status'], 'failed')
            pid = int((root / 'child.pid').read_text())
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                os.kill(pid, signal.SIGKILL)
                self.fail('Timed-out nested driver left its owned worker alive')
            self.assertEqual(json.loads((root / 'nested/process.json').read_text())['status'], 'failed')

if __name__ == '__main__':
    unittest.main()
