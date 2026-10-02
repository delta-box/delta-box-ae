"""CPU-only entry must preserve normal orchestration without admitting GPUs."""
from contextlib import nullcontext
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ae.scripts import run_review as review

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / 'ae/run_all_no_gpu.sh'


class CPUOnlyEntryTests(unittest.TestCase):
    def invoke(self, flags, *, status=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(ENTRY, root / ENTRY.name)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'ae/scripts/run_no_gpu_entry.sh', root / 'scripts/run_no_gpu_entry.sh')
            (root / 'run_all.sh').write_text(
                '#!/usr/bin/env bash\nprintf "%s\\0" "$@"\nexit ' + str(status) + '\n')
            fake = root / 'numactl'
            fake.write_text('#!/usr/bin/env bash\nshift 3\nexec "$@"\n')
            fake.chmod(0o755)
            environment = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']}
            environment.pop('AE_HOSTED_LAUNCHER', None)
            return subprocess.run(['bash', str(root / ENTRY.name), *flags], capture_output=True,
                                  env=environment)

    def test_normal_options_keep_argv_and_exit_status(self):
        options = ['--output', '/path with spaces/result', '--limit', '2', '--baseline-inputs', '44']
        result = self.invoke([*options, '--resume-failures', '0'], status=7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout.decode().split('\0')[:-1], ['--group', 'cpu', '--cpu-parallel', *options])

    def test_selection_and_internal_dispatch_cannot_enable_gpu(self):
        for flags in (['--group', 'gpu'], ['--group=gpu'], ['--gro=gpu'],
                      ['--experiment', 'figure-08-gpu'], ['--gpu-cases', 'generation-B1'],
                      ['--all'], ['--test'], ['--numa-node', '2'], ['--cpus=4-7'], ['--execute-plan', 'plan.json'],
                      ['--analyze-existing', 'old-gpu-run'], ['--']):
            with self.subTest(flags=flags):
                result = self.invoke(flags)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b'')

    def test_missing_values_and_help_do_not_launch(self):
        self.assertEqual(self.invoke(['--output']).returncode, 2)
        self.assertEqual(self.invoke(['--output', '--group=gpu']).returncode, 2)
        help_result = self.invoke(['--help'], status=9)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn(b'Figure 8(a)', help_result.stdout)

    def test_real_cpu_orchestration_never_probes_or_runs_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'config.json'
            config_path.write_text(json.dumps({'review': {'validation_max_jobs': 10},
                                                'gpu_remote_config': '/does/not/exist'}))
            args = review.parser().parse_args(['--group', 'cpu', '--config', str(config_path)])
            with patch.object(review, 'current_source', return_value={}):
                runner = review.Review(args, review.load_config(config_path), Path(directory) / 'result')
            self.assertEqual(runner.experiments, [e for e in review.EXPERIMENTS if e != review.GPU])
            self.assertEqual(runner.record['validation_max_jobs'], 10)
            self.assertEqual(runner.record['gpu_requested_cases'], [])
            visited = []
            def experiment(name):
                self.assertNotEqual(name, review.GPU)
                visited.append(name)
                runner.record['coverage'].append(dict(experiment=name, status='ok', successful_jobs=1))
            def step(name, *args, **kwargs):
                runner.record['steps'].append(dict(name=name, status='ok', log='fixture.log'))
                return True
            def analyze(source):
                self.assertIsNone(review.finish_gpu(runner, Path(directory) / 'analysis', True))
                step('paper-comparison')
            with patch('ae.scripts.figure08_remote.load_settings', side_effect=AssertionError('GPU config loaded')), \
                 patch('ae.scripts.figure08_remote.run_auto', side_effect=AssertionError('GPU probe/run attempted')), \
                 patch.object(runner, 'prepare_cube_service'), \
                 patch.object(review, 'load_runtime_environment'), \
                 patch.object(review, 'run_lock', side_effect=lambda *a, **k: nullcontext()), \
                 patch.object(runner, 'step', side_effect=step), \
                 patch.object(runner, 'run_experiment', side_effect=experiment), \
                 patch.object(runner, 'analyze', side_effect=analyze), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = runner.run()
            self.assertEqual(code, 0)
            self.assertEqual(visited, list(review.CPU_EXPERIMENTS))
            self.assertFalse((runner.output / 'gpu').exists())


class CPUReportScopeTests(unittest.TestCase):
    def runner(self, output, flags):
        config_path = output / 'config.json'
        config_path.write_text(json.dumps({'review': {'validation_max_jobs': 10}}))
        args = review.parser().parse_args(['--config', str(config_path), *flags])
        with patch.object(review, 'current_source', return_value={}):
            return review.Review(args, review.load_config(config_path), output)

    def test_unselected_gpu_is_out_of_scope_in_both_report_files(self):
        selections = [
            ['--group', 'cpu', '--cpu-parallel'],
            ['--group', 'cpu', '--cpu-parallel', '--cpu-layout', 'numa03'],
            ['--experiment', 'table-02-deltabox'],
        ]
        for flags in selections:
            for status in ('ok', 'failed'):
                with self.subTest(flags=flags, status=status), tempfile.TemporaryDirectory() as directory:
                    runner = self.runner(Path(directory), flags)
                    # Legacy records also carry the ambiguous skipped GPU placeholder.
                    runner.record['status'] = status
                    original_record = json.loads(json.dumps(runner.record))
                    with patch('ae.scripts.figure08_remote.report_lines', side_effect=AssertionError('GPU report rendered')):
                        runner.save()
                    self.assertEqual(json.loads((runner.output / 'review.json').read_text()), original_record)
                    summary = (runner.output / 'SUMMARY.md').read_text()
                    self.assertEqual(summary, (runner.output / 'result.md').read_text())
                    self.assertIn('outside the scope of this run', summary)
                    self.assertIn('Status: **' + status + '**', summary)
                    for text in ('automatic remote GPU', '0/8', 'Full GPU coverage requires', 'Host: `allinai2plus`'):
                        self.assertNotIn(text, summary)

    def test_selected_gpu_keeps_missing_coverage_and_failure_details(self):
        for status in ('skipped', 'failed'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                runner = self.runner(Path(directory), ['--group', 'gpu'])
                runner.record['gpu'].update(status=status, reason='original admission or measurement failure')
                runner.save()
                summary = (runner.output / 'SUMMARY.md').read_text()
                self.assertIn('automatic remote GPU measurement', summary)
                self.assertIn('Status: **' + status + '**; successful cases: 0/8.', summary)
                self.assertIn('Full GPU coverage requires all eight cases', summary)
                self.assertIn('original admission or measurement failure', summary)
                self.assertNotIn('outside the scope of this run', summary)

    def test_selected_gpu_success_keeps_coverage_report(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(Path(directory), ['--group', 'gpu'])
            runner.record['gpu'].update(status='complete', successful_cases=8, reason='all cases passed')
            runner.save()
            summary = (runner.output / 'SUMMARY.md').read_text()
            self.assertIn('Status: **complete**; successful cases: 8/8.', summary)
            self.assertNotIn('outside the scope of this run', summary)


if __name__ == '__main__':
    unittest.main()
