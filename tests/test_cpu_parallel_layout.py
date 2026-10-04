"""CPU layout admission/command tests; no VM, affinity or service changes."""
import argparse
import ast
from contextlib import contextmanager
import copy
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'ae/scripts/run_cpu_parallel.py'
spec = importlib.util.spec_from_file_location('cpu_parallel_layout_test', SCRIPT)
cpu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpu)


def cpu_experiments():
    source = ROOT / 'ae/repro/catalog.py'
    if not source.exists():  # The local candidate includes only changed files.
        source = ROOT / 'catalog-source.py'
    tree = ast.parse(source.read_text())
    value = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(name, ast.Name) and name.id == 'EXPERIMENTS' for name in node.targets))
    return ast.literal_eval(value)


def review_namespace():
    # Load the real argument/admission code without importing VM/backend modules.
    tree = ast.parse((ROOT / 'ae/scripts/run_review.py').read_text())
    names = {'gpu_case_selection', 'gpu_device_selection', 'GPUCases', 'parser', 'main', 'isolated_background_baseline'}
    tree.body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                 and node.name in names]
    catalogue = cpu_experiments()
    namespace = dict(argparse=argparse, Path=Path, os=os, REPO=ROOT,
                     EXPERIMENTS=catalogue, GROUPS={'cpu': list(catalogue)},
                     GPU_CASES=(), GPU='figure-08-gpu',
                     __name__='isolated_review', sys=types.SimpleNamespace(platform='linux', modules={'isolated_review': object()}))
    exec(compile(tree, 'run_review.py', 'exec'), namespace)
    return namespace


class LayoutTests(unittest.TestCase):
    def args(self, layout='numa12', **changes):
        args = review_namespace()['parser']().parse_args(['--group', 'cpu', '--cpu-parallel', '--cpu-layout', layout])
        for key, value in changes.items():
            setattr(args, key, value)
        return args

    def test_default_reviewer_layout_and_fixed_choices(self):
        parser = review_namespace()['parser']()
        args = parser.parse_args(['--group', 'cpu', '--cpu-parallel'])
        self.assertEqual(args.cpu_layout, 'numa12')
        self.assertEqual(cpu.placement(args), {1: '28-31', 2: '48-51'})
        with mock.patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(['--cpu-layout', 'custom'])

    def test_numa03_keeps_all_sixteen_group_candidates(self):
        args = self.args('numa03', limit=7, max_events=13)
        cpu.validate(args)
        self.assertEqual(cpu.placement(args), {0: '0-3', 3: '72-75'})
        catalogue = cpu_experiments()
        self.assertEqual(len(catalogue), 16)
        for node, cpus in cpu.placement(args).items():
            command = cpu.lane_arguments(args, node, ROOT / 'ae/results/selected/test', catalogue)
            self.assertEqual([command[i + 1] for i, x in enumerate(command) if x == '--experiment'], list(catalogue))
            self.assertEqual(command[command.index('--cpus') + 1], cpus)
            self.assertEqual(command[command.index('--cpu-layout') + 1], 'numa03')
            self.assertEqual(command[command.index('--limit') + 1], '7')
            self.assertEqual(command[command.index('--max-events') + 1], '13')

    def test_layout_commands_bind_cpu_and_memory_before_worker(self):
        args = self.args('numa03')
        assignments = {node: list(cpu_experiments()) for node in cpu.placement(args)}
        commands = cpu.lane_commands(args, ROOT / 'ae/results/selected/test', assignments, 42)
        self.assertEqual(set(commands), {0, 3})
        for node, command in commands.items():
            self.assertEqual(command[:4], ['numactl', '--all', '--physcpubind=' + cpu.placement(args)[node], '--membind=' + str(node)])
            boundary = command.index('--')
            self.assertIn('--cpu-layout', command[:boundary])
            self.assertIn('--cpu-layout', command[boundary + 1:])

    def test_custom_node_and_partial_selection_rejected(self):
        for changes in ({'numa_node': 1}, {'cpus': '0-23'}, {'group': ['table-02']}, {'experiment': ['correctness']}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                cpu.validate(self.args('numa03', **changes))

    def test_resume_layout_and_lane_cpus_cannot_change(self):
        experiments = cpu_experiments()
        numa12 = cpu.concurrency_policy(self.args(), experiments)
        numa03 = cpu.concurrency_policy(self.args('numa03'), experiments)
        self.assertFalse(cpu.validate_resume_policy(numa03, numa03, experiments))
        for previous, policy in [(numa12, numa03), (numa03, numa12)]:
            with self.assertRaises(ValueError):
                cpu.validate_resume_policy(previous, policy, experiments)
        changed = copy.deepcopy(numa03)
        changed['lanes']['0']['cpus'] = '4-7'
        with self.assertRaises(ValueError):
            cpu.validate_resume_policy(changed, numa03, experiments)

    def test_old_reviewer_shared_and_partitioned_resume_still_valid(self):
        experiments = cpu_experiments()
        policy = cpu.concurrency_policy(self.args(), experiments)
        old_shared = copy.deepcopy(policy)
        old_shared.pop('cpu_layout')
        self.assertFalse(cpu.validate_resume_policy(old_shared, policy, experiments))
        old_partition = dict(mode='cpu-two-lane', lanes={str(n): dict(node=n, cpus=cpus, experiments=cpu.partition(experiments)[n])
                             for n, cpus in cpu.PLACEMENT.items()}, trace_workers_per_lane=1)
        self.assertTrue(cpu.validate_resume_policy(old_partition, policy, experiments))
        with self.assertRaises(ValueError):
            cpu.validate_resume_policy(old_partition, cpu.concurrency_policy(self.args('numa03'), experiments), experiments)

    def test_worker_checks_actual_cpu_and_membind_including_node_zero(self):
        args = self.args('numa03', numa_node=0, cpus='0-3')
        cpu.validate_worker_binding(args, 0, 'numa03', affinity={0, 1, 2, 3}, memory_policy='policy: bind\nmembind: 0\n')
        for affinity, policy in [({0, 1, 2, 4}, 'policy: bind\nmembind: 0\n'),
                                 ({0, 1, 2, 3}, 'policy: default\nmembind: 0 1 2 3\n'),
                                 ({0, 1, 2, 3}, 'policy: bind\nmembind: 1\n')]:
            with self.subTest(affinity=affinity, policy=policy), self.assertRaises(ValueError):
                cpu.validate_worker_binding(args, 0, 'numa03', affinity=affinity, memory_policy=policy)
        with self.assertRaises(ValueError):
            cpu.validate_worker_binding(args, 0, 'numa12', affinity={0, 1, 2, 3}, memory_policy='policy: bind\nmembind: 0\n')

    def test_main_waits_for_exclusive_results_lease(self):
        ns = review_namespace()
        locks = []
        @contextmanager
        def lock(path, **kwargs):
            locks.append((path, kwargs))
            yield 42
        ns.update(validate_gpu_selection=lambda args: None, load_config=lambda path: {},
                  apply_validation_defaults=lambda args, config: None, run_lock=lock)
        modules = {}
        for name in ('cube_paper_profile', 'e2b_paper_profile'):
            module = types.ModuleType(name)
            module.validate = lambda args: None
            modules['ae.scripts.' + name] = module
        module = types.ModuleType('run_cpu_parallel')
        module.validate = cpu.validate
        module.run = mock.Mock(return_value=17)
        modules['ae.scripts.run_cpu_parallel'] = module
        with mock.patch.dict(sys.modules, modules):
            result = ns['main'](['--group', 'cpu', '--cpu-parallel', '--cpu-layout', 'numa03'])
        self.assertEqual(result, 17)
        self.assertEqual(locks, [(ROOT / 'ae/work/.results.lock', {'wait': True})])
        self.assertEqual(module.run.call_args.args[3], 42)

    def test_nondefault_layout_requires_parallel_selection(self):
        ns = review_namespace()
        ns['validate_gpu_selection'] = lambda args: None
        with mock.patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit) as raised:
            ns['main'](['--cpu-layout', 'numa03', '--list'])
        self.assertEqual(raised.exception.code, 2)

    def test_copied_wrapper_uses_new_layout_and_default_is_unchanged(self):
        original = ROOT / 'ae/run_all_no_gpu.sh'
        result = subprocess.run(['bash', str(original), '--help'], text=True, capture_output=True, check=True)
        self.assertIn('NUMA1 CPU28-31 and NUMA2 CPU48-51', result.stdout)
        script = ROOT / 'ae/run_all_no_gpu_numa03.sh'
        result = subprocess.run(['bash', str(script), '--help'], text=True, capture_output=True, check=True)
        self.assertIn('all 16', result.stdout)
        self.assertIn('reviewer requests priority', result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / script.name
            wrapper.write_text(script.read_text())
            (root / 'scripts').mkdir()
            (root / 'scripts/run_no_gpu_entry.sh').write_text((ROOT / 'ae/scripts/run_no_gpu_entry.sh').read_text())
            (root / 'run_all.sh').write_text("#!/bin/bash\nprintf '%s\\n' \"$@\"\n")
            fake = root / 'numactl'
            fake.write_text("#!/bin/bash\nprintf '%s\\n' \"$1\" \"$2\" \"$3\"\nshift 3\nexec \"$@\"\n")
            fake.chmod(0o755)
            sudo = root / 'sudo'
            sudo.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\"\n")
            sudo.chmod(0o755)
            env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']}
            env.pop('AE_HOSTED_LAUNCHER', None)
            result = subprocess.run(['bash', str(wrapper), '--output', '/tmp/test-output'], env=env, text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.splitlines(), ['--all', '--physcpubind=4-7', '--membind=0',
            '-n', '--', '/usr/local/sbin/deltabox-ae-run', '--checkout', str(root.parent),
            '--group', 'cpu', '--cpu-parallel', '--cpu-layout', 'numa03', '--output', '/tmp/test-output'])


if __name__ == '__main__':
    unittest.main()
