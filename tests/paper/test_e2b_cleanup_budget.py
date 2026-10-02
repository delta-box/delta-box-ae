"""Exercise the real runner command layers without launching any backend."""
import ast
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'ae/scripts/run_review.py'


def methods():
    tree = ast.parse(SOURCE.read_text())
    review = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Review')
    run = next(node for node in review.body if isinstance(node, ast.FunctionDef) and node.name == 'run_experiment')
    job = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'execute_review_job')
    namespace = dict(Path=Path, os=os, json=json, copy=copy, sys=sys, REPO=ROOT,
                     __file__=str(SOURCE), GPU='figure-08-gpu', from_environment=lambda: {})
    exec(compile(ast.Module(body=[run, job], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace


@contextlib.contextmanager
def hosted_environment(hosted):
    environment = dict(os.environ)
    environment.pop('AE_HOSTED_CALLER_UID', None)
    if hosted:
        environment['AE_HOSTED_CALLER_UID'] = '1001'
    with patch.dict(os.environ, environment, clear=True):
        yield


class E2BCleanupBudgetTests(unittest.TestCase):
    def outer_commands(self, experiment, hosted, node, *, execution='local'):
        namespace = methods()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            cpus = {0: '0-3', 1: '28-31', 2: '48-51', 3: '72-75'}[node]
            config = {'timeout': 10, 'measurement': {'pin': True, 'numa_node': node, 'cpus': cpus},
                      'e2b': {'execution': execution, 'api_url': 'http://127.0.0.1:3100',
                              'sandbox_url': 'http://127.0.0.1:3102'}, 'cube': {}}
            args = types.SimpleNamespace(config=out/'config.json', resume=None, baseline_inputs='44',
                                         cpu_parallel_lane=True, numa_node=node, cpus=cpus)
            job = {'key': experiment, 'experiment': experiment, 'command': ['producer'],
                   'run_purpose': 'full-trace', 'timeout_s': 10}
            calls, frozen = [], {}

            def write_json(path, value):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value))
                if 'plans' in path.parts:
                    frozen.update(copy.deepcopy(value))

            def step(name, command, timeout, **options):
                calls.append((name, command, options))
                return True

            def read_step(name):
                return {'jobs': [job]} if name.endswith('-plan') else [{'key': experiment, 'reasons': []}]

            namespace.update(load_config=lambda path: copy.deepcopy(config), deep_merge=lambda left, right: left,
                             pin_requested=lambda args, config: True,
                             measurement_placement=lambda args, config: {'node': node, 'cpus': cpus},
                             file_record=lambda path: {'path': str(path)}, write_json=write_json,
                             check_timeout=lambda config: 10, config_identity=lambda config: 'fixed',
                             bounded_plan_limits=lambda *args: [], validation_job_limit=lambda config: 10,
                             nvme_work_root=lambda config: None)
            runner = types.SimpleNamespace(args=args, config=config, overrides={}, output=out,
                attempt='attempt-001', record={'declared_unavailable': [], 'coverage': []},
                cube_disk_manifest=None, cube_memory_manifest=None, cube_placement=None,
                privilege=[], python='python', cli=['python', 'cli.py'], available=False,
                limits=[], save=lambda: None, step=step, read_step=read_step)
            cube = types.ModuleType('cube_paper_profile'); cube.effective = lambda config, profile: config
            e2b = types.ModuleType('e2b_paper_profile'); e2b.effective = lambda config, profile: config
            control = types.ModuleType('cube_control_context'); control.metadata_enabled = lambda config: False
            with hosted_environment(hosted), patch.dict(sys.modules, {
                    'ae.scripts.cube_paper_profile': cube,
                    'ae.scripts.e2b_paper_profile': e2b,
                    'ae.scripts.cube_control_context': control}):
                namespace['run_experiment'](runner, experiment)
            return next(row for row in calls if row[0] == experiment+'-run'), frozen

    def test_hosted_e2b_all_lanes_get_nested_restoration_budget(self):
        for node in (0, 1, 2, 3):
            with self.subTest(node=node):
                (_, command, options), plan = self.outer_commands('figure-08-e2b', True, node)
                self.assertTrue(plan['e2b_managed_service'])
                self.assertEqual(command[command.index('--node')+1], str(node))
                self.assertEqual(command[command.index('--stop-grace')+1], '360')
                self.assertEqual(options['termination_grace'], 420)

    def test_self_managed_and_unrelated_jobs_keep_existing_budget(self):
        for experiment, hosted, execution in (('figure-08-e2b', False, 'local'),
                                               ('figure-08-e2b', True, 'ssh'),
                                               ('figure-08-deltabox', True, 'local')):
            with self.subTest(experiment=experiment, hosted=hosted, execution=execution):
                (_, command, options), plan = self.outer_commands(experiment, hosted, 0, execution=execution)
                self.assertFalse(plan['e2b_managed_service'])
                self.assertEqual(command[command.index('--stop-grace')+1], '30')
                self.assertEqual(options['termination_grace'], 30)

    def test_inner_producer_budget_does_not_cut_short_hosted_e2b_restoration(self):
        for experiment, hosted, grace in (('figure-08-e2b', True, 300),
                                           ('figure-08-e2b', False, 30),
                                           ('figure-08-deltabox', True, 30)):
            with self.subTest(experiment=experiment, hosted=hosted):
                namespace = methods()
                execute = Mock(return_value={'status': 'failed'})
                namespace['execute'] = execute
                job = {'key': 'owned', 'experiment': experiment, 'command': ['producer'], 'run_purpose': 'full-trace'}
                plan = {'review_timeout': 10, 'jobs': [job],
                        'e2b_managed_service': experiment == 'figure-08-e2b' and hosted}
                with hosted_environment(hosted), contextlib.redirect_stdout(io.StringIO()):
                    namespace['execute_review_job'](1, job, plan, Path('/unused'))
                self.assertEqual(execute.call_args.kwargs['termination_grace'], grace)
                self.assertEqual(execute.call_args.kwargs['timeout'], 10)


if __name__ == '__main__':
    unittest.main()
