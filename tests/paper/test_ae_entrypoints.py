"""Exercise AE orchestration and resume integrity without a KVM/root host."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import venv
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('review_entry', ROOT / 'ae/scripts/run_review.py')
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)
SOURCE = {'source_commit': 'fixture', 'source_sha256': 'a' * 64}


class ReviewTests(unittest.TestCase):


    def test_cube_fanout_rechecks_daemon_identity_before_running(self):
        with patch('runners.cube_memory.verify', side_effect=ValueError('service identity changed')), \
             patch.object(review.socket, 'create_connection'):
            reasons = review.job_unavailable(
                dict(experiment='figure-08-cube', command=['python', 'fanout.py']),
                {'baseline_storage': 'tmpfs', 'cube': {'api_url': 'http://127.0.0.1:3000'}})
        self.assertIn('Cube memory service verification failed: service identity changed', reasons)

    def test_cube_manifest_is_prepared_before_any_experiment_and_restored(self):
        events = []
        @contextlib.contextmanager
        def context(output, **kwargs):
            output.mkdir(parents=True)
            manifest = output / 'storage.json'
            manifest.write_text('{}')
            events.append(('prepared', kwargs))
            try:
                yield manifest
            finally:
                events.append(('restored', None))
        config = {'cube': {'manage_memory_service': True, 'memory_size_gib': 16},
                  'measurement': {'pin': True, 'numa_node': 1, 'cpus': '28-31'}}
        with patch('ae.scripts.cube_memory_context.memory_service', side_effect=context), \
             patch('runners.cube_memory.verify', return_value={}):
            code, record, commands, files = self.exercise([], config_extra=config)
        self.assertEqual(code, 0)
        self.assertEqual(events[0], ('prepared', {'node': 1, 'cpus': '28-31', 'size_gib': 16}))
        self.assertEqual(events[-1][0], 'restored')
        for name in ('table-02-cube', 'figure-08-cube'):
            effective = json.loads(files[f'configs/attempt-001/{name}.json'])
            self.assertTrue(effective['cube']['memory_manifest'].endswith('/cube-memory/storage.json'))

    def test_cube_setup_failure_prevents_earlier_cpu_measurements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'config.json'
            config_path.write_text(json.dumps({'cube': {'manage_memory_service': True}}))
            args = review.parser().parse_args(['--config', str(config_path)])
            with patch.object(review, 'current_source', return_value=SOURCE):
                runner = review.Review(args, review.load_config(config_path), root / 'out')
            with patch('ae.scripts.cube_memory_context.memory_service', side_effect=ValueError('busy Cube')), \
                 patch.object(review, 'execute') as execute:
                with self.assertRaisesRegex(ValueError, 'busy Cube'):
                    runner.run()
            execute.assert_not_called()
            self.assertEqual(runner.record['status'], 'failed')

    def test_cube_restoration_failure_is_not_a_successful_run(self):
        @contextlib.contextmanager
        def context(output, **kwargs):
            output.mkdir(parents=True)
            path = output / 'storage.json'
            path.write_text('{}')
            yield path
            raise RuntimeError('restoration failed')
        with patch('ae.scripts.cube_memory_context.memory_service', side_effect=context), \
             patch('runners.cube_memory.verify', return_value={}):
            code, record, _, _ = self.exercise([], config_extra={'cube': {'manage_memory_service': True}})
        self.assertEqual((code, record['status']), (1, 'failed'))
        self.assertEqual(record['steps'][-1]['name'], 'cube-service-cleanup')

    def test_default_baseline_selection_reaches_each_effective_config(self):
        for flags, selected in [([], '44'), (['--baseline-inputs', 'all'], 'all')]:
            code, record, _, files = self.exercise(flags)
            self.assertEqual(code, 0)
            self.assertEqual(record['baseline_inputs'], selected)
            for backend in ('replay', 'criu', 'fc-diff'):
                config = json.loads(files[f'configs/attempt-001/table-02-{backend}.json'])
                self.assertEqual(config['baseline_inputs'], selected)
            self.assertNotIn('baseline_inputs', json.loads(files['configs/attempt-001/table-02-cube.json']))

    def test_resume_rejects_changing_the_input_set(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(review, 'current_source', return_value=SOURCE):
            root = Path(tmp)
            old = review.Review(review.parser().parse_args(['--baseline-inputs', 'all']), {}, root)
            old.save()
            with self.assertRaisesRegex(ValueError, 'baseline input set differs'):
                review.Review(review.parser().parse_args(['--resume', str(root)]), {}, root)

    def test_quick_check_entrypoint_forwards_paths_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake = base / 'python'
            fake.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(7)\n')
            fake.chmod(0o755)
            flags = ['--config', str(base / 'config with spaces.json'),
                     '--output', str(base / 'output with spaces')]
            env = {**os.environ, 'AE_PYTHON': str(fake), 'AE_HOSTED_LAUNCHER': ''}
            completed = subprocess.run(['bash', str(ROOT / 'ae/run_test.sh'), *flags],
                                       cwd=base, env=env, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 7, completed.stderr)
            self.assertEqual(json.loads(completed.stdout),
                             [str(ROOT / 'ae/scripts/run_review.py'), '--test', *flags])

    def test_legacy_check_option_is_supported_but_not_advertised(self):
        self.assertEqual(vars(review.parser().parse_args(['--test'])),
                         vars(review.parser().parse_args(['--smoke'])))
        self.assertNotIn('--smoke', review.parser().format_help())
        identity = {'source_commit': 'a' * 40, 'source_sha256': 'b' * 64}
        with patch.object(review, 'current_source', return_value=identity):
            path = review.default_output(review.parser().parse_args(['--test']))
        self.assertEqual(path.parent, ROOT / 'ae/results/checks')
        self.assertTrue(path.name.startswith('quick-check-'))

    def test_summary_links_existing_bilingual_pages_for_current_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(review, "current_source", return_value=SOURCE):
                runner = review.Review(review.parser().parse_args(["--test"]), {}, root)
            old = root / "comparison/attempt-001"
            old.mkdir(parents=True)
            for filename in ("README.md", "README-zh.md"):
                (old / filename).write_text("previous attempt")
            runner.attempt = "attempt-002"
            runner.save()
            self.assertNotIn("Paper comparison / 论文对比", (root / "SUMMARY.md").read_text())
            current = root / "comparison" / runner.attempt
            current.mkdir()
            for filename in ("README.md", "README-zh.md"):
                (current / filename).write_text("current attempt")
            runner.save()
            text = (root / "SUMMARY.md").read_text()
            self.assertIn("[English](comparison/attempt-002/README.md)", text)
            self.assertIn("[简体中文](comparison/attempt-002/README-zh.md)", text)
            self.assertNotIn("comparison/attempt-001/", text)

    def test_default_result_layout_keeps_latest_and_separates_checks(self):
        identity = {'source_commit': 'a' * 40, 'source_sha256': 'b' * 64}
        with patch.object(review, 'current_source', return_value=identity):
            full = review.default_output(review.parser().parse_args([]))
            subset = review.default_output(review.parser().parse_args(['--limit', '1']))
            short = review.default_output(review.parser().parse_args(['--max-events', '3']))
        self.assertEqual(full, ROOT / 'ae/results')
        self.assertEqual(subset.parent, ROOT / 'ae/results/selected')
        self.assertEqual(short.parent, ROOT / 'ae/results/selected')

    def exercise(self, flags, failures=(), missing=(), *, config_extra=None, interrupt=None, timing=None, gpu_failure=False):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / 'config.json'
            config.write_text(json.dumps({'timeout': 1, **(config_extra or {})}))
            out = base / 'result'
            out.mkdir()
            args = review.parser().parse_args(['--config', str(config), *flags])
            with patch.object(review, 'current_source', return_value=SOURCE):
                runner = review.Review(args, review.load_config(config), out)
            commands = {}
            plans = {}

            def fake_execute(argv, output, **kwargs):
                name = output.name
                commands[name] = argv
                output.mkdir(parents=True)
                content = {}
                if name.endswith('-doctor'):
                    content = {'ok': name not in failures, 'checks': [{'name': 'fixture', 'ok': name not in failures, 'detail': 'missing fixture dependency'}]}
                elif name.endswith('-plan'):
                    exp = name.removesuffix('-plan')
                    suite = Path(argv[argv.index('--output') + 1])
                    content = {'jobs': [{'experiment': exp, 'key': exp + '__' + str(n),
                                        'command': ['python', 'fixture.py', '--out', str(suite / (exp + '__' + str(n)))],
                                        'run_purpose': 'quick-check' if runner.limits else 'full-cohort', **(timing or {})} for n in range(2)]}
                    plans[exp] = content
                elif name.endswith('-inputs'):
                    exp = name.removesuffix('-inputs')
                    content = [{'key': job['key'], 'reasons': ['missing repo'] if job['key'] in missing else []} for job in plans[exp]['jobs']]
                elif name.endswith('-run'):
                    path = Path(argv[argv.index('--execute-plan') + 1])
                    plan = json.loads(path.read_text())
                    suite = Path(plan['review_output'])
                    suite.mkdir(parents=True)
                    for job in plan['jobs']:
                        job['status'] = 'failed' if name in failures else 'ok'
                    review.write_json(suite / 'suite.json', plan)
                (output / 'stdout.log').write_text(json.dumps(content))
                if name == interrupt:
                    raise KeyboardInterrupt()
                return {'status': 'failed' if name in failures else 'ok', 'returncode': (2 if name.endswith('-doctor') else 1) if name in failures else 0}

            def fake_gpu(runner):
                runner.record['coverage'].append(dict(experiment='figure-08-gpu', optional=False, status='failed' if gpu_failure else 'ok',
                                                       planned_jobs=8, available_jobs=8, successful_jobs=0 if gpu_failure else 8))
            with patch.object(review, 'execute', side_effect=fake_execute), \
                    patch.object(review.Review, 'run_gpu', autospec=True, side_effect=fake_gpu), \
                    patch.object(review, 'finish_gpu', return_value=None), contextlib.redirect_stdout(io.StringIO()):
                if interrupt:
                    with self.assertRaises(KeyboardInterrupt):
                        runner.run()
                    code = 130
                else:
                    code = runner.run()
            files = {str(p.relative_to(out)): p.read_text() for p in out.rglob('*.json')}
            return code, json.loads((out / 'review.json').read_text()), commands, files

    def test_explicit_available_records_missing_without_claiming_full_pass(self):
        code, record, commands, _ = self.exercise(['--available'], failures={'table-02-cube-doctor'})
        self.assertEqual(code, 0)
        self.assertEqual(record['status'], 'ok-with-unavailable')
        self.assertEqual(record['selection_mode'], 'available')
        self.assertEqual(record['experiments'], list(review.EXPERIMENTS))
        self.assertNotIn('table-02-cube-run', commands)
        self.assertIn('correctness-run', commands)
        cube = next(row for row in record['coverage'] if row['experiment'] == 'table-02-cube')
        self.assertEqual(cube['status'], 'unavailable')
        self.assertIn('missing fixture dependency', cube['reasons'][0])

    def test_default_and_all_require_complete_cpu_dependencies(self):
        for flags in ([], ['--all']):
            with self.subTest(flags=flags):
                code, record, commands, _ = self.exercise(flags, failures={'table-02-cube-doctor'})
                self.assertEqual((code, record['status']), (1, 'failed'))
                self.assertEqual(record['selection_mode'], 'required')
                self.assertEqual(record['run_purpose'], 'ae-cohorts')
                self.assertEqual(record['experiments'], list(review.EXPERIMENTS))
                self.assertNotIn('table-02-cube-run', commands)
                self.assertNotIn('correctness-run', commands)
                cube = next(row for row in record['coverage'] if row['experiment'] == 'table-02-cube')
                self.assertEqual(cube['status'], 'unavailable')
                self.assertIn('missing fixture dependency', cube['reasons'][0])

    def test_default_complete_cpu_gpu_run_succeeds(self):
        code, record, _, _ = self.exercise([])
        self.assertEqual((code, record['status']), (0, 'ok'))
        self.assertEqual(record['selection_mode'], 'required')
        self.assertEqual(record['experiments'], list(review.EXPERIMENTS))
        self.assertTrue(all(row['status'] == 'ok' and row['successful_jobs'] == row['planned_jobs']
                            for row in record['coverage']))

    def test_required_gpu_failure_preserves_cpu_evidence_but_fails_run(self):
        code, record, commands, _ = self.exercise([], gpu_failure=True)
        self.assertEqual((code, record["status"]), (1, "failed"))
        cpu = [row for row in record["coverage"] if row["experiment"] != "figure-08-gpu"]
        self.assertTrue(all(row["status"] == "ok" for row in cpu))
        self.assertNotIn("paper-comparison", commands)

    def test_gpu_only_does_not_attempt_to_analyze_nonexistent_cpu_runs(self):
        code, record, commands, _ = self.exercise(["--group", "gpu"])
        self.assertEqual((code, record["status"]), (0, "ok"))
        self.assertNotIn("analyze", commands)
        self.assertNotIn("plot", commands)
        self.assertIn("paper-comparison", commands)

    def test_actual_failure_stops_remaining_experiments(self):
        for flags in ([], ['--all'], ['--available']):
            with self.subTest(flags=flags):
                code, record, commands, _ = self.exercise(flags, failures={'table-02-criu-run'})
                self.assertEqual((code, record['status']), (1, 'failed'))
                self.assertNotIn('correctness-run', commands)
                self.assertNotIn('analyze', commands)
                self.assertNotIn('paper-comparison', commands)
                self.assertTrue(any(row['status'] == 'not-run' for row in record['coverage']))

    def test_partial_input_selection_is_reported_as_full_trace_not_full_cohort(self):
        code, record, _, files = self.exercise(['--available', '--experiment', 'table-02-replay'], missing={'table-02-replay__1'})
        self.assertEqual(code, 0)
        row = record['coverage'][0]
        self.assertEqual((row['status'], row['planned_jobs'], row['available_jobs']), ('partial', 2, 1))
        plan = json.loads(files['plans/attempt-001/table-02-replay.json'])
        self.assertEqual(plan['jobs'][0]['run_purpose'], 'full-trace')
        self.assertEqual(len(plan['unavailable_jobs']), 1)

    def test_required_partial_inputs_fail_for_default_all_and_selected_groups(self):
        for flags in ([], ['--all'], ['--experiment', 'table-02-replay'], ['--group', 'baselines']):
            with self.subTest(flags=flags):
                code, record, _, _ = self.exercise(flags, missing={'table-02-replay__1'})
                self.assertEqual((code, record['status']), (1, 'failed'))
                row = next(row for row in record['coverage'] if row['experiment'] == 'table-02-replay')
                self.assertEqual((row['status'], row['planned_jobs'], row['successful_jobs']), ('partial', 2, 1))

    def test_quick_check_stays_small_and_defaults_to_requested_numa_frequency_controls(self):
        code, record, commands, _ = self.exercise(['--test'])
        self.assertEqual(code, 0)
        self.assertEqual(record['experiments'], ['table-02-deltabox'])
        self.assertEqual(record['run_purpose'], 'quick-check')
        self.assertEqual(commands['table-02-deltabox-plan'][-4:], ['--limit', '1', '--max-events', '3'])
        command = commands['table-02-deltabox-run']
        self.assertEqual(command[command.index('--node') + 1], '2')
        self.assertEqual(command[command.index('--cpus') + 1], '52-55')
        self.assertIn(str(ROOT / 'ae/scripts/run_pinned_measurement.py'), command)

    def test_no_pin_requires_explicit_flag(self):
        _, record, commands, _ = self.exercise(['--test', '--no-pin'])
        self.assertFalse(record['pinned'])
        self.assertNotIn('--node', commands['table-02-deltabox-run'])

    def test_self_built_config_can_disable_host_pin_without_cli_flag(self):
        _, record, commands, _ = self.exercise(['--test'], config_extra={'measurement': {'pin': False}})
        self.assertFalse(record['pinned'])
        self.assertFalse(record['coverage'][0]['measurement']['pinned'])
        self.assertNotIn('--node', commands['table-02-deltabox-run'])

    def test_explicit_pin_flags_enable_unless_no_pin_overrides(self):
        for flags, expected in [(['--numa-node', '0', '--cpus', '0-3'], True),
                                (['--numa-node', '0', '--no-pin'], False)]:
            _, record, commands, _ = self.exercise(['--test', *flags], config_extra={'measurement': {'pin': False}})
            self.assertEqual(record['coverage'][0]['measurement']['pinned'], expected)
            self.assertEqual('--node' in commands['table-02-deltabox-run'], expected)

    def test_each_experiment_honors_its_effective_pin_configuration(self):
        _, record, commands, _ = self.exercise(['--group', 'deltabox'], config_extra={
            'measurement': {'pin': False}, 'review': {'experiment_overrides': {'table-03-slow': {'measurement': {'pin': True}}}}})
        self.assertFalse(record['coverage'][0]['measurement']['pinned'])
        self.assertTrue(record['coverage'][1]['measurement']['pinned'])
        self.assertNotIn('--node', commands['table-02-deltabox-run'])
        self.assertIn('--node', commands['table-03-slow-run'])

    def test_deep_merge_preserves_unmodified_configuration(self):
        _, _, _, files = self.exercise(['--experiment', 'figure-06-memory'], config_extra={
            'checkpoint_profile': 'async-incremental', 'measurement': {'numa_node': 2, 'cpus': '52-55'},
            'review': {'experiment_overrides': {'figure-06-memory': {'checkpoint_profile': 'runtime-default', 'figure06_memory_policies': ['none', 'skip', 'gc']}}}})
        config = json.loads(files['configs/attempt-001/figure-06-memory.json'])
        self.assertEqual(config['checkpoint_profile'], 'runtime-default')
        self.assertEqual(config['measurement']['numa_node'], 2)
        self.assertEqual(config['figure06_memory_policies'], ['none', 'skip', 'gc'])
        self.assertNotIn('review', config)

    def test_effective_config_preserves_relative_nested_paths_and_image_overrides(self):
        _, _, _, files = self.exercise(['--experiment', 'table-02-deltabox'], config_extra={
            'instance_data_images': {'django__django-14672': 'private/data-django.xfs'},
            'cube': {'sdk': 'private/sdk'}, 'baseline_test_runtime': {'python': 'private/python'},
            'e2b': {'fanout_python': 'private/sdk-python'}})
        config = json.loads(files['configs/attempt-001/table-02-deltabox.json'])
        source = Path(config['_config_dir'])
        self.assertEqual(config['instance_data_images']['django__django-14672'], str(source / 'private/data-django.xfs'))
        self.assertEqual(config['cube']['sdk'], str(source / 'private/sdk'))
        self.assertEqual(config['baseline_test_runtime']['python'], str(source / 'private/python'))
        self.assertEqual(config['e2b']['fanout_python'], str(source / 'private/sdk-python'))

    def test_effective_config_preserves_python_virtual_environments(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = Path(tmp).resolve() / 'sdk-venv'
            venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
            python = environment / 'bin/python'
            self.assertTrue(python.is_symlink())
            _, _, _, files = self.exercise(['--experiment', 'figure-08-e2b'], config_extra={
                'e2b': {'fanout_python': str(python)},
                'baseline_test_runtime': {'python': str(python)}})
            config = json.loads(files['configs/attempt-001/figure-08-e2b.json'])
            for group, key in [('e2b', 'fanout_python'), ('baseline_test_runtime', 'python')]:
                with self.subTest(interpreter=group + '.' + key):
                    # Execute the serialized path: resolving bin/python to its
                    # system target silently discards the virtual environment.
                    prefix = subprocess.check_output(
                        [config[group][key], '-c', 'import sys; print(sys.prefix)'], text=True).strip()
                    self.assertEqual(Path(prefix), environment)

    def test_failed_analysis_still_generates_missing_comparison_panels(self):
        code, _, commands, _ = self.exercise(['--test'], failures={'analyze'})
        self.assertEqual(code, 1)
        self.assertNotIn('plot', commands)
        self.assertIn('paper-comparison', commands)
        self.assertNotIn('--analysis', commands['paper-comparison'])

    def test_comparison_coverage_is_immutable_after_live_review_finishes(self):
        _, record, commands, files = self.exercise(['--test'])
        coverage = record['comparison_coverage']
        snapshot = files['coverage/attempt-001/review.json']
        self.assertEqual(hashlib.sha256(snapshot.encode()).hexdigest(), coverage['sha256'])
        self.assertEqual(json.loads(snapshot)['status'], 'measurement-coverage-snapshot')
        command = commands['paper-comparison']
        self.assertEqual(Path(command[command.index('--coverage') + 1]).resolve(), Path(coverage['path']))
        self.assertNotEqual(hashlib.sha256(files['review.json'].encode()).hexdigest(), coverage['sha256'])

    def test_interruption_does_not_continue_measurements_or_analyze(self):
        _, record, commands, _ = self.exercise([], interrupt='table-02-deltabox-run')
        self.assertEqual(record['status'], 'interrupted')
        self.assertNotIn('table-03-slow-run', commands)
        self.assertNotIn('analyze', commands)
        self.assertNotIn('paper-comparison', commands)

    def test_failed_dependency_and_interruption_preserve_failure(self):
        code, record, commands, _ = self.exercise(['--test'], failures={'verify'})
        self.assertEqual((code, record['status']), (1, 'failed'))
        self.assertEqual(set(commands), {'prepare', 'verify'})
        code, record, _, _ = self.exercise(['--test'], interrupt='table-02-deltabox-run')
        self.assertEqual((code, record['status']), (130, 'interrupted'))
        self.assertEqual(record['steps'][-1]['status'], 'interrupted')
        self.assertEqual(record['coverage'][-1]['status'], 'interrupted')

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            config.write_text('{}')
            with patch.dict(review.os.environ, {}), patch.object(review.sys, 'platform', 'linux'), patch.object(review, 'current_source', return_value=SOURCE):
                self.assertEqual(review.main(['--config', str(config), '--output', tmp]), 2)
            self.assertFalse((Path(tmp) / 'review.json').exists())

    def test_resume_rejects_changed_source_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review.write_json(root / 'review.json', {'release': {'source_sha256': 'different'}})
            args = review.parser().parse_args(['--resume', str(root)])
            with patch.object(review, 'current_source', return_value=SOURCE):
                with self.assertRaisesRegex(ValueError, 'fingerprint'):
                    review.Review(args, {}, root)

    def test_default_and_all_resume_the_same_required_selection(self):
        for original_flags, resumed_flags in (([], []), (['--all'], []), ([], ['--all'])):
            with self.subTest(original=original_flags, resumed=resumed_flags), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                args = review.parser().parse_args(original_flags)
                with patch.object(review, 'current_source', return_value=SOURCE):
                    original = review.Review(args, {}, root)
                    original.save()
                    resumed_args = review.parser().parse_args([*resumed_flags, '--resume', str(root)])
                    resumed = review.Review(resumed_args, {}, root)
                self.assertEqual(resumed.record['selection_mode'], 'required')
                self.assertEqual(resumed.record['run_purpose'], original.record['run_purpose'])
                self.assertEqual(resumed.attempt, 'attempt-002')

    def test_available_resume_requires_explicit_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(review, 'current_source', return_value=SOURCE):
                original = review.Review(review.parser().parse_args(['--available']), {}, root)
                original.save()
                args = review.parser().parse_args(['--resume', str(root)])
                with self.assertRaisesRegex(ValueError, 'selection/purpose'):
                    review.Review(args, {}, root)
                args = review.parser().parse_args(['--available', '--resume', str(root)])
                resumed = review.Review(args, {}, root)
            self.assertEqual(resumed.record['selection_mode'], 'available')
            self.assertEqual(resumed.record['run_purpose'], 'available-cohorts')
            self.assertEqual(resumed.attempt, 'attempt-002')

    def test_resume_rejects_replaced_image_at_the_same_configured_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'source.xfs'
            image.write_bytes(b'old-image')
            manifest = {'images': {'data_xfs': {'path': str(image), 'sha256': hashlib.sha256(image.read_bytes()).hexdigest()}}}
            with patch.object(review, 'REPO', Path(tmp)):
                review.verify_reused_images(manifest)
                image.write_bytes(b'new-image')
                with self.assertRaisesRegex(ValueError, 'source image changed'):
                    review.verify_reused_images(manifest)

    def test_resume_same_stat_rewrite_does_not_trust_old_or_aged_racy_hash(self):
        from replay import provenance
        for legacy_cache in (False, True):
            with self.subTest(legacy_cache=legacy_cache), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                image, cache = root / 'disk.xfs', root / 'ae/work/image-hashes.json'
                image.write_bytes(b'original')
                identity = provenance.signature(image)
                stamp = max(identity['mtime_ns'], identity['ctime_ns'])
                with patch.object(provenance.time, 'time_ns', return_value=stamp):
                    original = provenance.cached_digest(image, cache)
                if legacy_cache:
                    cache.write_text(json.dumps({str(image.resolve()): original}))
                manifest = {'images': {'data_xfs': original}}
                image.write_bytes(b'modified')
                # Preserve all five stat fields, then advance the clock:
                # neither the old manifest nor an aged ambiguous cache is proof.
                with patch.object(review, 'REPO', root),\
                     patch.object(provenance, 'signature', return_value=identity),\
                     patch.object(provenance.time, 'time_ns', return_value=stamp + 10 * provenance._RACY_STAT_NS):
                    with self.assertRaisesRegex(ValueError, 'source image changed'):
                        review.verify_reused_images(manifest, cache=cache)

    def test_resume_reuses_one_verified_stable_hash_across_job_manifests(self):
        from replay import provenance
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / 'disk.xfs'
            image.write_bytes(b'original')
            identity = provenance.signature(image)
            stamp = max(identity['mtime_ns'], identity['ctime_ns'])
            manifest = {'images': {'data_xfs': {**identity, 'sha256': hashlib.sha256(b'original').hexdigest()}}}
            cache = root / 'resume-images.json'
            with patch.object(review, 'REPO', root),\
                 patch.object(provenance.time, 'time_ns', return_value=stamp + 2 * provenance._RACY_STAT_NS),\
                 patch.object(provenance, 'file_digest', wraps=provenance.file_digest) as hash_file:
                review.verify_reused_images(manifest, cache=cache)
                review.verify_reused_images(manifest, cache=cache)
                hash_file.assert_called_once_with(image.resolve())

    def test_resume_uses_writable_output_cache_without_opening_producer_cache(self):
        from replay import provenance
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            producer = root / 'repo/ae/work/image-hashes.json'
            producer.parent.mkdir(parents=True)
            lock = producer.with_suffix('.json.lock')
            for path in (producer, lock):
                path.write_text('privileged producer cache')
                path.chmod(0)
            if review.os.geteuid() != 0:
                with self.assertRaises(PermissionError):
                    with lock.open('a'):
                        pass
            image = root / 'disk.xfs'
            image.write_bytes(b'original')
            image.chmod(0o444)
            identity = provenance.signature(image)
            stamp = max(identity['mtime_ns'], identity['ctime_ns'])
            manifest = {'images': {'data_xfs': {**identity, 'sha256': hashlib.sha256(b'original').hexdigest()}}}
            output = root / 'output'
            output.mkdir()
            cache = output / f'.resume-image-hashes-{review.os.geteuid()}.json'
            with patch.object(review, 'REPO', root / 'repo'),\
                 patch.object(provenance.time, 'time_ns', return_value=stamp + 2 * provenance._RACY_STAT_NS),\
                 patch.object(provenance, 'file_digest', wraps=provenance.file_digest) as hash_file:
                review.verify_reused_images(manifest, cache=cache)
                review.verify_reused_images(manifest, cache=cache)
                hash_file.assert_called_once_with(image.resolve())
            self.assertTrue(cache.is_file())
            for path in (producer, lock):
                self.assertEqual(path.stat().st_mode & 0o777, 0)

    def test_successful_resume_validates_hash_and_retains_failed_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite = root / 'runs/fixture'
            jobs = []
            for key, status in [('success', 'ok'), ('failure', 'failed')]:
                work = suite / key
                work.mkdir(parents=True)
                (work / 'result.txt').write_text('real evidence')
                review.write_json(work / 'run.json', dict(status=status, analysis_mode='fresh-measurement', experiment='fixture', artifacts=[dict(path='result.txt', bytes=13, sha256=hashlib.sha256(b'real evidence').hexdigest())]))
                jobs.append(dict(key=key, command=['echo', key], run_purpose='full-trace', status=status))
            review.write_json(suite / 'suite.json', dict(effective_config_sha256='config', jobs=jobs))
            args = review.parser().parse_args([])
            with patch.object(review, 'current_source', return_value=SOURCE):
                runner = review.Review(args, {}, root)
            plan = dict(effective_config_sha256='config', jobs=[{k: v for k, v in j.items() if k != 'status'} for j in jobs])
            row = dict(experiment='fixture')
            with patch.object(review, 'verify_reused_images', wraps=review.verify_reused_images) as verify:
                runner.prepare_resume(plan, suite, row)
                self.assertEqual(verify.call_args.kwargs['cache'],
                    root / f'.resume-image-hashes-{review.os.geteuid()}.json')
            self.assertEqual(row['reused_jobs'], ['success'])
            self.assertTrue(plan['jobs'][0]['reused_verified'])
            self.assertTrue((root / 'failed-attempts/attempt-001/fixture/failure/result.txt').is_file())
            (suite / 'success/result.txt').write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                runner.prepare_resume(plan, suite, row)

    def test_pinning_envelope_sums_per_job_deadlines(self):
        _, record, commands, _ = self.exercise(['--test'], timing={'timeout_s': 1000, 'recorded_wait_s': 500})
        command = commands['table-02-deltabox-run']
        self.assertEqual(float(command[command.index('--timeout') + 1]), 2240)
        self.assertEqual(record['coverage'][0]['recorded_wait_s'], 1000)

    def test_only_structured_missing_prerequisites_can_be_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'stdout.log'
            log.write_text('Traceback: release source mismatch')
            self.assertIsNone(review.prerequisite_failure(log, 2))
            log.write_text(json.dumps(dict(ok=False, checks=[dict(name='kvm', ok=False, detail='missing /dev/kvm')])))
            self.assertIsNone(review.prerequisite_failure(log, 1))
            self.assertEqual(review.prerequisite_failure(log, 2), ['kvm: missing /dev/kvm'])

    def test_no_measurement_cannot_succeed_even_if_plotting_returns_zero(self):
        code, record, commands, _ = self.exercise(['--available', '--experiment', 'table-02-replay'],
                                                 failures={'table-02-replay-doctor'})
        self.assertEqual((code, record['status']), (1, 'failed'))
        self.assertIn('paper-comparison', commands)

    def test_analysis_only_does_not_verify_current_measurement_source_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = review.parser().parse_args(['--analyze-existing', tmp])
            with patch.object(review, 'current_source', side_effect=ValueError('new source differs')),\
                 patch.object(review, 'working_source', return_value=SOURCE):
                runner = review.Review(args, {}, Path(tmp) / 'new')
            self.assertEqual(runner.record['release'], {})
            self.assertEqual(runner.record['analyzer'], SOURCE)

    def test_analysis_only_can_render_existing_missing_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            coverage = [dict(experiment='table-02-cube', status='unavailable', reasons=['missing service'])]
            review.write_json(source / 'review.json', dict(release=SOURCE, run_purpose='available-cohorts', coverage=coverage))
            with patch.object(review, 'working_source', return_value=SOURCE):
                code, record, commands, _ = self.exercise(['--analyze-existing', str(source)])
        self.assertEqual((code, record['status']), (0, 'ok-with-unavailable'))
        self.assertEqual(record['run_purpose'], 'analysis-only')
        self.assertEqual(record['coverage'], coverage)
        self.assertEqual(set(commands), {'plot-dependencies', 'analyze', 'plot', 'paper-comparison'})

    def test_measurement_records_actual_source_without_selecting_a_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / 'config.json'
            config.write_text('{}')
            with patch.dict(review.os.environ, {}, clear=True), \
                 patch.object(review.sys, 'platform', 'linux'), \
                 patch.object(review, 'from_environment', return_value=SOURCE), \
                 patch.object(review.Review, 'run', return_value=0), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.main(['--config', str(config), '--output', str(base / 'new')]), 0)
                self.assertNotIn('DELTABOX_RELEASE_LOCK', review.os.environ)

    def test_stale_explicit_lock_does_not_block_measurement_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / 'config.json'
            config.write_text('{}')
            with patch.dict(review.os.environ, {'DELTABOX_RELEASE_LOCK': '/missing/stale-lock.json'}), \
                 patch.object(review.sys, 'platform', 'linux'), \
                 patch.object(review.Review, 'run', return_value=0), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.main(['--config', str(config), '--output', str(base / 'new')]), 0)
                self.assertTrue((base / 'new').is_dir())

    def test_list_and_analysis_do_not_select_or_verify_current_release_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(review.os.environ, {'DELTABOX_RELEASE_LOCK': '/missing/stale-lock.json'}), \
                 patch.object(review, 'from_environment', side_effect=AssertionError('measurement only')), \
                 patch.object(review, 'working_source', return_value=SOURCE), \
                 patch.object(review.Review, 'run', return_value=0), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.main(['--list']), 0)
                self.assertEqual(review.main(['--analyze-existing', str(base), '--output', str(base / 'new')]), 0)

    def test_published_output_does_not_follow_symlinks_or_make_configs_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'output'
            output.mkdir()
            outside = root / 'outside'
            outside.write_text('keep private')
            outside.chmod(0o400)
            (output / 'external').symlink_to(outside)
            secret = output / 'config.json'
            secret.write_text('{}')
            secret.chmod(0o600)
            nested = output / 'nested'
            nested.mkdir(mode=0o000)
            review.make_output_accessible(output)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o400)
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
            self.assertEqual(nested.stat().st_mode & 0o700, 0o700)

    def test_hosted_outputs_remain_root_owned_and_cannot_be_modified_by_reviewers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'output'
            output.mkdir()
            result = output / 'result'
            result.write_text('measurement')
            result.chmod(0o6777)
            config = output / 'config.json'
            config.write_text('{}')
            config.chmod(0o600)
            outside = root / 'outside'
            outside.write_text('not an output')
            outside.chmod(0o666)
            (output / 'source').symlink_to(outside)
            with patch.dict(review.os.environ, {'AE_HOSTED_CALLER_UID': '7001', 'SUDO_UID': '7001', 'SUDO_GID': '7001'}),\
                 patch.object(review.os, 'geteuid', return_value=0), patch.object(review.os, 'chown') as chown:
                review.make_output_accessible(output)
            self.assertTrue(all(call.args[1:3] == (0, 0) for call in chown.call_args_list))
            self.assertNotIn(output / 'source', [call.args[0] for call in chown.call_args_list])
            self.assertEqual(result.stat().st_mode & 0o6777, 0o755)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o666)

    def test_execute_plan_stops_after_job_failure_and_persists_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / 'plan.json'
            suite = root / 'suite'
            review.write_json(plan, dict(review_output=str(suite), review_timeout=1, jobs=[
                dict(key='bad', command=['false'], run_purpose='full-trace', timeout_s=1000, recorded_wait_s=500),
                dict(key='good', command=['true'], run_purpose='full-trace')]))
            calls, budgets = [], []
            def execute(argv, output, **kwargs):
                calls.append(argv)
                budgets.append(kwargs['timeout'])
                return dict(status='failed' if argv == ['false'] else 'ok')
            with patch.object(review, 'execute', side_effect=execute), patch.object(review, 'repository_state', return_value={}),\
                 patch.object(review, 'host_state', return_value={}), patch.object(review, 'from_environment', return_value=SOURCE),\
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.execute_plan(plan), 1)
            self.assertEqual(calls, [['false']])
            self.assertEqual(budgets, [1000])
            result = json.loads((suite / 'suite.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual([job['status'] for job in result['jobs']], ['failed', 'not-run'])

    def test_staging_cleanup_failure_stops_next_producer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / 'plan.json'
            suite = root / 'suite'
            review.write_json(plan, dict(review_output=str(suite), review_timeout=1, jobs=[
                dict(key=key, command=[key], run_purpose='full-trace') for key in ('good', 'cleanup-error', 'bad')]))
            order = []
            def execute(argv, output, **kwargs):
                order.append('producer-exited:' + argv[0])
                return dict(status='failed' if argv == ['bad'] else 'ok')
            def cleanup(path):
                order.append('cleanup:' + path.name)
                if path.name == 'cleanup-error':
                    raise ValueError('artifact conflict')
                return dict(status='ok', removed=[])
            with patch.object(review, 'execute', side_effect=execute), patch.object(review, 'repository_state', return_value={}),\
                 patch.object(review, 'host_state', return_value={}), patch.object(review, 'from_environment', return_value=SOURCE),\
                 patch('repro.staging_cleanup.cleanup_reconstructable_staging', side_effect=cleanup),\
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.execute_plan(plan), 1)
            self.assertEqual(order, ['producer-exited:good', 'cleanup:good',
                                     'producer-exited:cleanup-error', 'cleanup:cleanup-error'])
            result = json.loads((suite / 'suite.json').read_text())
            self.assertEqual([job['status'] for job in result['jobs']], ['ok', 'failed', 'not-run'])
            self.assertIn('artifact conflict', result['jobs'][1]['staging_cleanup']['error'])

    def test_interrupted_cleanup_does_not_record_an_ok_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / 'plan.json'
            suite = root / 'suite'
            review.write_json(plan, dict(review_output=str(suite), review_timeout=1, jobs=[
                dict(key='fixture', command=['true'], run_purpose='full-trace')]))
            with patch.object(review, 'execute', return_value=dict(status='ok')),\
                 patch.object(review, 'repository_state', return_value={}),\
                 patch.object(review, 'host_state', return_value={}),\
                 patch.object(review, 'from_environment', return_value=SOURCE),\
                 patch('repro.staging_cleanup.cleanup_reconstructable_staging', side_effect=KeyboardInterrupt()),\
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    review.execute_plan(plan)
            result = json.loads((suite / 'suite.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['jobs'][0]['status'], 'cleaning')

    def test_probe_rejects_incompatible_async_figure6_and_missing_image(self):
        reasons = review.job_unavailable(dict(experiment='figure-06-memory', command=['python', '--data-xfs', '/nonexistent-image']), {'checkpoint_profile': 'async-incremental'})
        self.assertTrue(any('Missing file' in reason for reason in reasons))
        self.assertTrue(any('standard replay only' in reason for reason in reasons))

    def test_war_probe_only_downgrades_confirmed_missing_input(self):
        from repro.repositories import LocalObjectReadError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'data-tools.xfs').touch()
            actions = root / 'actions.json'
            actions.write_text(json.dumps(dict(instance_id='owner__project-1', base_commit='a' * 40, edits=[{'file_path': 'a.py'}])))
            job = dict(experiment='figure-09', command=['python', '--actions', str(actions)])
            config = dict(payload=str(root), images_dir=str(root))
            with patch('repro.repositories.select_repository', side_effect=FileNotFoundError('missing cached blob')):
                self.assertEqual(review.job_unavailable(job, config), ['missing cached blob'])
            with patch('repro.repositories.select_repository', side_effect=LocalObjectReadError('owned Git timed out')):
                with self.assertRaisesRegex(LocalObjectReadError, 'timed out'):
                    review.job_unavailable(job, config)
            with patch('repro.repositories.select_repository', side_effect=ValueError('invalid recorded commit')):
                with self.assertRaisesRegex(ValueError, 'invalid recorded commit'):
                    review.job_unavailable(job, config)

    def test_old_checkout_requires_explicit_runtime_before_python_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp)
            (old / 'ae/scripts').mkdir(parents=True)
            shutil.copy2(ROOT / 'ae/run_all.sh', old / 'ae/run_all.sh')
            (old / 'ae/scripts/run_review.py').write_text('raise RuntimeError("old runtime must not run")\n')
            result = subprocess.run(['bash', str(old / 'ae/run_all.sh'), '--list'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('--runtime-repo PATH', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            forwarded = subprocess.run(['bash', str(old / 'ae/run_all.sh'), '--runtime-repo', str(ROOT), '--list'], capture_output=True, text=True)
            self.assertEqual(forwarded.returncode, 0, forwarded.stderr)
            self.assertIn('table-02-deltabox', forwarded.stdout)

    def test_runtime_repo_option_is_explicit_and_checked(self):
        result = subprocess.run(['bash', str(ROOT / 'ae/run_all.sh'), '--runtime-repo', str(ROOT), '--list'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Runtime checkout: ' + str(ROOT), result.stdout)
        self.assertIn('table-02-deltabox', result.stdout)
        bad = subprocess.run(['bash', str(ROOT / 'ae/run_all.sh'), '--runtime-repo', '/nonexistent'], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)


if __name__ == '__main__':
    unittest.main()
