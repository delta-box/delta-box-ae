"""Remote admission, provenance and required one-click GPU behavior without CUDA."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ae.scripts import figure08_remote as remote
from ae.scripts import run_review as review
from ae.runners import gpu_timing


class RemoteTests(unittest.TestCase):
    def settings(self):
        config = remote.load_settings(remote.DEFAULT_CONFIG)
        config['sample_interval_s'] = 0
        return config

    def test_hosted_gpu_ssh_uses_policy_account_without_key_copy(self):
        from types import SimpleNamespace
        import pwd
        with patch.dict(os.environ, {'AE_HOSTED_CALLER_UID': '1012', 'AE_HOSTED_GPU_SSH_USER': 'dyp'}, clear=True), \
             patch.object(remote.os, 'geteuid', return_value=0), \
             patch.object(pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1010, pw_name='dyp')):
            command = remote.ssh_transport(self.settings())
        self.assertEqual(command[:7], ['sudo', '-n', '-H', '-u', 'dyp', '--', 'ssh'])

    def test_nonroot_cannot_select_hosted_gpu_ssh_account(self):
        with patch.dict(os.environ, {'AE_HOSTED_CALLER_UID': '1012', 'AE_HOSTED_GPU_SSH_USER': 'dyp'}, clear=True), \
             patch.object(remote.os, 'geteuid', return_value=1012):
            command = remote.ssh_transport(self.settings())
        self.assertEqual(command[0], 'ssh')

    def observation(self, count=8):
        return dict(gpus=[dict(index=i, uuid=f'GPU-{i}', memory_mib=4, utilization_pct=0)
                          for i in range(count)], processes=[])

    def test_idle_requires_stable_identity_no_pid_and_low_memory(self):
        config = self.settings()
        obs = [self.observation() for _ in range(3)]
        obs[0]['processes'] = [dict(pid=123, uuid='GPU-0')]
        obs[1]['gpus'][1]['memory_mib'] = 86000
        obs[2]['gpus'][2]['utilization_pct'] = 70
        obs[2]['gpus'][3]['uuid'] = 'GPU-changed'
        obs[2]['gpus'][4]['memory_mib'] = float('nan')
        self.assertEqual([g['index'] for g in remote.idle_devices(obs, config)], [5, 6, 7])
        self.assertEqual(remote.idle_devices(obs[:1], config), [])

    def test_single_and_full_matrix(self):
        self.assertEqual(remote.suites_for([]), [])
        for n in (1, 2, 3, 4, 8):
            suites = remote.suites_for([str(i) for i in range(n)])
            self.assertEqual(sum(len(batches) for _, batches, _ in suites), 8 if n >= 4 else 6)
            for phase, batches, devices in suites:
                if batches == [16, 64]:
                    self.assertEqual((phase, len(devices)), ('training', 4))

    def remote_root(self, root):
        config = self.settings()
        config['remote_root'] = str(root / 'shared')
        remote.write_json(root / 'remote-config.json', config)
        remote.write_json(root / 'source.json', {'files': {}})
        return config

    def test_all_busy_skips_without_loading_or_checking_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.remote_root(root)
            with patch.object(remote, 'probe', return_value=dict(idle=[], observations=[])), \
                    patch.object(gpu_timing, 'check_resources') as check, \
                    patch.object(gpu_timing, 'run_suite') as run:
                result = remote.remote_run(root)
            self.assertEqual(result['status'], 'skipped')
            self.assertIn('No idle', result['reason'])
            check.assert_not_called()
            run.assert_not_called()
            self.assertTrue((root / 'results/remote.json').is_file())

    def test_resource_race_skips_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.remote_root(root)
            initial = dict(idle=[dict(index=0, uuid='GPU-0')], observations=[])
            with patch.object(remote, 'probe', side_effect=[initial, dict(idle=[], observations=[])]), \
                    patch.object(gpu_timing, 'run_suite') as run:
                result = remote.remote_run(root)
            self.assertEqual(result['status'], 'skipped')
            self.assertIn('changed', result['reason'])
            run.assert_not_called()
            with (Path(config['remote_root']) / 'locks/GPU-0.lock').open('a') as lock:
                remote.fcntl.flock(lock, remote.fcntl.LOCK_EX | remote.fcntl.LOCK_NB)

    def test_another_coordinator_lock_is_respected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.remote_root(root)
            locks = Path(config['remote_root']) / 'locks'
            locks.mkdir(parents=True)
            with (locks / 'GPU-0.lock').open('a') as lock:
                remote.fcntl.flock(lock, remote.fcntl.LOCK_EX)
                with patch.object(remote, 'probe', return_value=dict(idle=[dict(index=0, uuid='GPU-0')], observations=[])):
                    result = remote.remote_run(root)
            self.assertEqual(result['selected'], [])
            self.assertEqual(result['status'], 'skipped')

    def test_ssh_unavailable_is_a_recorded_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            with patch.object(remote, 'snapshot', return_value=Path(directory) / 'archive'), \
                    patch.object(remote, 'ssh', side_effect=subprocess.CalledProcessError(255, ['ssh'], stderr=b'host unavailable')):
                result = remote.run_auto(output)
            self.assertEqual(result['status'], 'skipped')
            self.assertEqual(result['detail'], 'host unavailable')
            self.assertEqual(json.loads((output / 'manifest.json').read_text()), result)

    def test_remote_arguments_are_quoted_and_batch_mode_enabled(self):
        config = self.settings()
        with patch.object(remote.os, 'geteuid', return_value=1000), patch.object(remote.subprocess, 'run') as run:
            remote.ssh(config, ['python', '/path with space/$(touch marker)'])
        argv = run.call_args.args[0]
        self.assertIn('BatchMode=yes', argv)
        self.assertEqual(remote.shlex.split(argv[-1]), ['python', '/path with space/$(touch marker)'])

    def test_version_drift_is_a_failed_preflight_check(self):
        check = dict(ok=True, checks=[], software=dict(generation=dict(packages=dict(vllm='0.19.0'))))
        remote.check_versions(check, self.settings(), 'generation')
        self.assertFalse(check['ok'])
        self.assertEqual(check['checks'][-1]['detail']['vllm']['expected'], '0.21.0')

    def test_remote_partial_and_full_execution_plans(self):
        for count in (1, 4):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.remote_root(root)
                admission = dict(idle=[dict(index=i, uuid=f'GPU-{i}') for i in range(count)], observations=[])
                calls = []
                def run(config, output, **kwargs):
                    calls.append((config['phases'], config['batches'], config['devices']))
                    return dict(status='ok')
                with patch.object(remote, 'probe', return_value=admission), \
                        patch.object(gpu_timing, 'check_resources', side_effect=lambda c: dict(ok=True, checks=[], software={})), \
                        patch.object(gpu_timing, 'run_suite', side_effect=run):
                    result = remote.remote_run(root)
                self.assertEqual(result['status'], 'measured')
                self.assertEqual(sum(len(b) for _, b, _ in calls), 6 if count == 1 else 8)
                if count == 4:
                    self.assertEqual(calls[-1], (['training'], [16, 64], ['GPU-0', 'GPU-1', 'GPU-2', 'GPU-3']))

    def test_environment_is_restored(self):
        config = self.settings()
        config['generation_env'] = {'LD_LIBRARY_PATH': '/new/lib'}
        with patch.dict(os.environ, {'LD_LIBRARY_PATH': 'old'}):
            with remote.phase_environment(config, 'generation'):
                self.assertEqual(os.environ['LD_LIBRARY_PATH'], config['generation_env']['LD_LIBRARY_PATH'])
            self.assertEqual(os.environ['LD_LIBRARY_PATH'], 'old')

    def evidence(self, root):
        source = dict(files={'ae/runners/gpu_worker.py': 'worker', 'ae/repro/gpu_protocol.py': 'protocol'})
        folder = root / '01-generation'
        folder.mkdir()
        config = remote.protocol.load_config(model_path='/models/qwen', devices=['GPU-0'],
                                             phases=['generation'], batches=[1])
        remote.write_json(folder / 'config.json', config)
        config_sha = remote.digest(folder / 'config.json')
        case = remote.protocol.cases(config)[0]
        raw = dict(schema_version=1, kind='gpu-timing-result', status='ok', gpu_verified=True,
                   **case, config_sha256=config_sha, worker_source_sha256='worker', protocol_source_sha256='protocol',
                   samples=[dict(rep=i, total_s=.25) for i in range(case['reps'])],
                   hardware=[dict(device=0, uuid='GPU-0', name='fixture', total_memory_bytes=80*1024**3, capability=[9, 0])],
                   software=dict(ok=True, python='3.12', python_executable='/python',
                                 packages=dict(torch='2.4', vllm='0.8', transformers='4.51')),
                   cuda_version='12.4', torch_version='2.4')
        raw_path = folder / 'cases/generation-B1/result.json'
        remote.write_json(raw_path, raw)
        row = dict(case, status='ok', result=dict(path='cases/generation-B1/result.json',
                   sha256=remote.digest(raw_path), bytes=raw_path.stat().st_size))
        suite = dict(status='ok', cases=[row], config_sha256=config_sha, protocol=config, model_identity=dict(sha256='model'))
        remote.write_json(folder / 'summary.json', suite)
        report = dict(suites=[dict(path='01-generation/summary.json', status='ok', sha256=remote.digest(folder / 'summary.json'))])
        return source, report, raw_path

    def test_collected_timings_recomputed_and_partial_plot_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, report, raw = self.evidence(root)
            combined = remote.collect_timings(root, report, source)
            self.assertEqual(combined['coverage_status'], 'partial')
            self.assertEqual(combined['cases'][0]['timing_s']['mean'], .25)
            self.assertEqual(len(combined['missing_cases']), 7)
            from ae.repro.gpu_occupation import indexed_gpu_cases
            self.assertEqual(len(indexed_gpu_cases(combined)), 1)
            raw.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'hash/size'):
                remote.collect_timings(root, report, source)

    def test_remote_partial_plot_enters_bilingual_comparison_without_theory(self):
        from types import SimpleNamespace
        from ae.scripts.build_review_comparison import figure08_supplement, supplemental_markdown
        from repro.review_gpu import FANOUT, GPU
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'gpu/attempt-001'
            evidence = run / 'results'
            evidence.mkdir(parents=True)
            source, report, _ = self.evidence(evidence)
            report['source'] = source
            remote.write_json(run / 'source.json', source)
            remote.write_json(evidence / 'remote.json', report)
            subject = SimpleNamespace(output=root, attempt='attempt-001', limits=[], record=dict(
                gpu_output='gpu/attempt-001', gpu=dict(status='partial', successful_cases=1, reason='Missing other cases'),
                release={}, outputs={}, experiments=[*FANOUT, GPU], coverage=[dict(experiment=n, status='ok') for n in FANOUT]))
            result = remote.finish_remote(subject, root / 'analysis', False)
            panels = figure08_supplement(result)['panels']
            self.assertEqual([p['status'] for p in panels], ['partial', 'unavailable'])
            for language in ('en', 'zh'):
                self.assertIn('![Figure 8(b)', '\n'.join(supplemental_markdown(panels, language=language)))
            self.assertTrue(subject.record['coverage'][-1]['optional'])

    def test_remote_full_inputs_preserve_automatic_theory(self):
        from types import SimpleNamespace
        from repro.review_gpu import FANOUT, GPU
        from repro.common import stats
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'gpu/attempt-001'
            evidence = run / 'results'
            evidence.mkdir(parents=True)
            remote.write_json(run / 'source.json', {})
            remote.write_json(evidence / 'remote.json', dict(source={}))
            cases = []
            config = remote.protocol.load_config()
            for case in remote.protocol.cases(config):
                cases.append(dict(case, status='ok', timing_s=stats([1.0] * case['reps']),
                                  result=dict(path='fixture.json', sha256='a' * 64, bytes=1)))
            suite = dict(schema_version=1, kind='gpu-timing-suite', source_kind='fresh', status='ok',
                         model_label='fixture', cases=cases)
            def collect(*args):
                remote.write_json(evidence / 'summary.json', suite)
                return suite
            analysis = root / 'analysis'
            series = [dict(panel='a', backend=b, x=n, y=100, unit='ms', estimated=False,
                           plot_group='fixture', source_identity='release-sha256:' + 'b' * 64)
                      for b in ('deltabox', 'cube', 'e2b') for n in (1, 4, 16, 64)]
            remote.write_json(analysis / 'summary.json', dict(schema_version=1, source='fresh',
                experiments={'figure-08': dict(status='analyzed', series=series)}))
            subject = SimpleNamespace(output=root, attempt='attempt-001', limits=[], record=dict(
                gpu_output='gpu/attempt-001', gpu=dict(status='complete', successful_cases=8),
                release={}, outputs={}, experiments=[*FANOUT, GPU], coverage=[dict(experiment=n, status='ok') for n in FANOUT]))
            with patch.object(remote, 'collect_timings', side_effect=collect):
                metadata = remote.finish_remote(subject, analysis, True)
            panels = json.loads(metadata.read_text())['panels']
            self.assertEqual([p['status'] for p in panels], ['ok', 'ok'])
            occupation = json.loads((run / 'comparison/theory/occupation.json').read_text())
            self.assertEqual(len(occupation['rows']), 6)

    def test_failed_suite_is_never_published_even_with_successful_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, report, raw = self.evidence(root)
            report['suites'][0]['status'] = 'failed'
            self.assertEqual(remote.collect_timings(root, report, source)['cases'], [])

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                remote.checked_path(Path(directory), '../result.json')


class ReviewIntegrationTests(unittest.TestCase):
    def runner(self, root, argv=()):
        args = review.parser().parse_args(list(argv))
        with patch.object(review, 'current_source', return_value={}):
            return review.Review(args, {}, root)

    def test_all_busy_fails_required_run_and_preserves_cpu_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = self.runner(root)
            def step(name, *args, **kwargs):
                runner.record['steps'].append(dict(name=name, status='ok', log='fixture.log'))
                return True
            def experiment(name):
                if name == review.GPU:
                    return runner.run_gpu()
                runner.record['coverage'].append(dict(experiment=name, status='ok', successful_jobs=1))
            def analyze(source):
                step('paper-comparison')
            with patch.object(runner, 'prepare_cube_service'), \
                    patch.object(runner, 'step', side_effect=step), \
                    patch.object(runner, 'run_experiment', side_effect=experiment), \
                    patch.object(runner, 'analyze', side_effect=analyze), \
                    patch.object(remote, 'run_auto', return_value=dict(status='skipped', reason='All GPUs busy', successful_cases=0)) as gpu:
                self.assertEqual(runner.run(), 1)
            gpu.assert_called_once()
            self.assertIn('All GPUs busy', (root / 'result.md').read_text())
            self.assertEqual(runner.record['status'], 'failed')

    def test_analysis_only_copies_gpu_evidence_without_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = root / 'prior'
            evidence = prior / 'gpu/attempt-001'
            evidence.mkdir(parents=True)
            (evidence / 'manifest.json').write_text('{}')
            remote.write_json(prior / 'review.json', dict(gpu=dict(status='skipped', reason='All GPUs busy'),
                                                         gpu_output='gpu/attempt-001'))
            output = root / 'analysis'
            args = review.parser().parse_args(['--analyze-existing', str(prior)])
            with patch.object(review, 'working_source', return_value={}):
                runner = review.Review(args, {}, output)
            def analyze(source):
                runner.record['steps'].append(dict(name='paper-comparison', status='ok', log='fixture'))
            with patch.object(runner, 'analyze', side_effect=analyze), patch.object(remote, 'run_auto') as gpu:
                self.assertEqual(runner.run(), 0)
            gpu.assert_not_called()
            self.assertTrue((output / 'gpu/imported/manifest.json').is_file())
            self.assertIn('All GPUs busy', (output / 'result.md').read_text())

    def test_service_environment_is_scoped_and_not_copied_into_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'environment.json'
            path.write_text(json.dumps(dict(E2B_API_KEY='fixture-secret', PATH='/untrusted')))
            config = dict(environment_file=str(path))
            with patch.dict(os.environ, {}, clear=True):
                review.load_runtime_environment(config, ['figure-08-cube'])
                self.assertNotIn('E2B_API_KEY', os.environ)
                review.load_runtime_environment(config, ['figure-08-e2b'])
                self.assertEqual(os.environ['E2B_API_KEY'], 'fixture-secret')
                self.assertNotIn('PATH', os.environ)
                self.assertNotIn('fixture-secret', json.dumps(config))

    def test_unselected_and_smoke_never_open_ssh(self):
        for args in (['--test'], ['--smoke'], ['--group', 'table-02'], ['--max-events', '3']):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                runner = self.runner(Path(directory), args)
                with patch.object(remote, 'run_auto') as gpu:
                    runner.run_gpu()
                gpu.assert_not_called()

    def test_gpu_failure_preserves_cpu_report_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(Path(directory), ['--group', 'figure-08'])
            with patch.object(remote, 'run_auto', side_effect=RuntimeError('transport failed')):
                runner.run_gpu()
            self.assertEqual(runner.record['gpu']['status'], 'failed')
            self.assertIn('transport failed', (runner.output / 'result.md').read_text())
