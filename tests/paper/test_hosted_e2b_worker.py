"""Exercise the actual E2B plan and retained metadata without starting a VM."""
import argparse
import copy
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae/runners'))
sys.path.insert(0, str(ROOT))
from ae.scripts.run_review import deep_merge

spec = importlib.util.spec_from_file_location('hosted_e2b_baseline', ROOT / 'ae/runners/baseline.py')
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)
import e2b_environment


class HostedE2BWorkerTests(unittest.TestCase):
    def config(self):
        value = json.loads((ROOT / 'ae/configs/spr4numa-review.json').read_text())
        return deep_merge(value, value['review']['experiment_overrides']['table-02-e2b'])

    def plan(self, config):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = copy.deepcopy(config)
            venv = root / 'venv'
            (venv / 'bin').mkdir(parents=True)
            (venv / 'bin/python').write_text('fixture')
            config['moatless_venv'] = str(venv)
            trace = root / 'trajectory.json'
            trace.write_text('{}')
            output = root / 'result'
            def stage(*args, **kwargs):
                payload = output / 'payload'
                traces = output / 'mock_traces'
                payload.mkdir(); traces.mkdir()
                return payload, traces, []
            args = argparse.Namespace(backend='e2b', instance='django__django-10914',
                collect_phases=False, experiment_id=None, config=root / 'config.json',
                out=output, trace=trace, limit=None, repository_commit=None,
                timeout=60, dry_run=True)
            with ExitStack() as stack:
                for name, result in [('load_config', config), ('host_state', {}),
                                     ('repository_state', {}), ('from_environment', {}),
                                     ('configure_test_runtime', {}), ('stage_local_dependencies', [])]:
                    stack.enter_context(patch.object(baseline, name, return_value=result))
                stack.enter_context(patch.object(baseline, 'stage_payload', side_effect=stage))
                stack.enter_context(patch.object(e2b_environment, 'configure', return_value={'storage': '/fixture/storage'}))
                launch = stack.enter_context(patch.object(baseline, 'execute', side_effect=AssertionError('dry-run must not execute')))
                memory = {'storage_mode': 'tmpfs-noswap', 'node': 1, 'cpus': [28, 29, 30, 31]} if config.get('baseline_storage') == 'tmpfs' else None
                stack.enter_context(patch.dict(baseline.os.environ,
                    {'AE_MEMORY_JOB': json.dumps(memory) if memory else ''}))
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(baseline.run(args), 0)
                launch.assert_not_called()
            return json.loads((output / 'run.json').read_text())

    def test_hosted_override_produces_cold_driver_and_retains_method_evidence(self):
        config = self.config()
        self.assertIs(config['e2b']['warm_action_worker'], False)
        record = self.plan(config)
        self.assertNotIn('--warm-action-worker', record['command'])
        self.assertEqual(record['e2b_worker_mode'], 'cold')
        self.assertEqual(record['status'], 'planned')
        self.assertIs(record['config']['e2b']['warm_action_worker'], False)
        self.assertEqual(record['config']['e2b']['execution'], config['e2b']['execution'])
        self.assertEqual(record['config']['e2b']['from_build'], config['e2b']['from_build'])
        self.assertIs(record['recorded_search_order'], config['recorded_search_order'])
        self.assertEqual(record['storage_mode'], 'tmpfs-noswap')
        self.assertEqual(record['memory_backing']['node'], 1)
        self.assertEqual(record['memory_backing']['cpus'], [28, 29, 30, 31])
        self.assertEqual(config['memory_job_size_gib'], 24)

    def test_explicit_warm_control_is_still_available_and_labeled(self):
        config = self.config()
        config['e2b']['warm_action_worker'] = True
        record = self.plan(config)
        self.assertIn('--warm-action-worker', record['command'])
        self.assertEqual(record['e2b_worker_mode'], 'warm')
        self.assertIs(record['config']['e2b']['warm_action_worker'], True)


if __name__ == '__main__':
    unittest.main()
