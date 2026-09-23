"""Every figure producer carries the campaign lock before any measured work."""
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'ae'), str(ROOT / 'ae/runners')]
from release import lock
from repro.analysis import Evidence, FreshRun, source_identity
from repro.common import file_record, write_json


def load(name):
    spec = importlib.util.spec_from_file_location('release_fixture_' + name, ROOT / 'ae/runners' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VM = load('vm_experiment')
FANOUT = load('fanout')


class ProducerReleaseBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.checkout = self.base / 'locked-checkout'
        source = self.checkout / 'ae/runners/fixture.py'
        source.parent.mkdir(parents=True)
        source.write_text('frozen = True\n')
        for arguments in (['init', '-q'], ['add', '.'],
                          ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture']):
            subprocess.run(['git', '-C', str(self.checkout), *arguments], check=True, capture_output=True)
        self.lock_path = self.base / 'candidate-lock.json'
        write_json(self.lock_path, lock.create(self.checkout))
        self.release = self.verify()
        self.images = self.base / 'images'
        self.images.mkdir()
        for name in ('kernel', 'base.xfs', 'data-tools.xfs'):
            (self.images / name).write_bytes(b'fixture image identity')
        self.config = self.base / 'config.json'
        write_json(self.config, dict(kernel=str(self.images / 'kernel'), base_xfs=str(self.images / 'base.xfs'),
            images_dir=str(self.images), moatless_venv=str(self.base / 'venv'),
            cube=dict(sdk=str(self.base / 'sdk'), phase_binary=str(self.base / 'cubelet'),
                      api_url='http://localhost:3000', template='fixture', proxy_node_ip='127.0.0.1'),
            e2b=dict(api_url='http://localhost:3000', sandbox_url='http://localhost:3002', template='fixture')))

    def verify(self):
        return lock.verify(self.lock_path, root=self.checkout)

    def assert_bound(self, output):
        run = FreshRun(Evidence(output, 'fresh'), output / 'run.json', set())
        self.assertEqual(run.config['release'], self.release)
        self.assertEqual(source_identity(run.config), 'release-sha256:' + self.release['source_sha256'])
        self.assertEqual(run.config['runtime']['commit'], 'later-doc-commit')
        self.assertEqual(len(run.artifacts), 1)
        for name, path in run.artifacts.items():
            expected = next(record for record in run.config['artifacts'] if record['path'] == name)
            self.assertEqual(expected['sha256'], file_record(path)['sha256'])
        return run.config

    def common(self, stack, module, arguments):
        stack.enter_context(patch.object(sys, 'argv', [module.__file__, '--config', str(self.config), *arguments]))
        stack.enter_context(patch.object(module, 'from_environment', side_effect=self.verify))
        stack.enter_context(patch.object(module, 'repository_state', return_value=dict(commit='later-doc-commit', tracked_diff_sha256='b' * 64)))
        stack.enter_context(patch.object(module, 'host_state', return_value={}))
        stack.enter_context(redirect_stdout(io.StringIO()))

    def test_all_vm_panels_bind_the_release_and_keep_measurement_artifacts(self):
        for experiment in ('figure-08-deltabox', 'figure-09', 'correctness'):
            with self.subTest(experiment=experiment), ExitStack() as stack:
                output = self.base / experiment
                arguments = ['--experiment', experiment, '--out', str(output), '--forks', '1']
                if experiment == 'figure-09':
                    arguments += ['--actions', str(self.base / 'actions.json'), '--arm', 'ext4', '--input-key', 'fixture']
                self.common(stack, VM, arguments)
                stack.enter_context(patch.object(VM, 'build_guest_archive', return_value={'fixture_source_sha256': 'c' * 64}))
                stack.enter_context(patch.object(VM, 'build_extra', return_value=([], dict(experiment=experiment, expected_edits=1))))
                stack.enter_context(patch.object(VM, 'cached_digest', side_effect=lambda path, cache: file_record(path)))
                def measured(argv, logdir, **kwargs):
                    manifest = json.loads((output / 'run.json').read_text())
                    self.assertEqual(manifest['release'], self.release)
                    self.assertEqual(source_identity(manifest), 'release-sha256:' + self.release['source_sha256'])
                    measurements = output / 'measurements'
                    measurements.mkdir()
                    if experiment == 'figure-08-deltabox':
                        write_json(measurements / 'fanout.json', [dict(forks=1, success=True, success_count=1, ready_e2e_ms=2.0)])
                    elif experiment == 'figure-09':
                        (measurements / 'fixture_ext4.jsonl').write_text(json.dumps(dict(file_size_bytes=100, copyup_bytes=4096, phys_bytes=4096)) + '\n')
                    else:
                        write_json(measurements / 'correctness.json', dict(ok=True))
                    return dict(status='ok')
                stack.enter_context(patch.object(VM, 'execute', side_effect=measured))
                self.assertEqual(VM.main(), 0)
                self.assert_bound(output)

    def test_external_fanout_backends_bind_the_same_release(self):
        for backend in ('cube', 'e2b'):
            with self.subTest(backend=backend), ExitStack() as stack:
                output = self.base / backend
                self.common(stack, FANOUT, ['--backend', backend, '--out', str(output), '--forks', '1'])
                if backend == 'cube':
                    stack.enter_context(patch('cube_environment.capture_cube_environment',
                                              return_value={'template_cpu_millicores': 500, 'template_memory_mb': 512}))
                else:
                    stack.enter_context(patch.object(FANOUT, 'probe_e2b_sdk',
                                              return_value={'ok': True, 'api_key': 'set', 'sdk': {'version': 'fixture'}}))
                def measured(argv, logdir, **kwargs):
                    manifest = json.loads((output / 'run.json').read_text())
                    self.assertEqual(manifest['release'], self.release)
                    if backend == 'cube':
                        self.assertEqual(manifest['cube_environment']['template_memory_mb'], 512)
                    write_json(output / 'fanout.json', [dict(forks=1, success=True, success_count=1, ready_e2e_ms=3.0)])
                    return dict(status='ok')
                stack.enter_context(patch.object(FANOUT, 'execute', side_effect=measured))
                self.assertEqual(FANOUT.main(), 0)
                self.assert_bound(output)

    def test_changed_locked_source_stops_both_producers_before_output_or_execution(self):
        (self.checkout / 'ae/runners/fixture.py').write_text('frozen = False\n')
        for module, arguments in ((VM, ['--experiment', 'correctness']), (FANOUT, ['--backend', 'cube'])):
            with self.subTest(module=module.__name__), ExitStack() as stack:
                output = self.base / module.__name__
                self.common(stack, module, [*arguments, '--out', str(output)])
                execute = stack.enter_context(patch.object(module, 'execute'))
                with self.assertRaisesRegex(ValueError, 'release source mismatch'):
                    module.main()
                execute.assert_not_called()
                self.assertFalse(output.exists())

    def test_guest_launch_rejects_a_different_manifest_release_before_vm_start(self):
        config = self.base / 'guest.json'
        write_json(config, dict(release=dict(self.release, source_sha256='d' * 64)))
        with patch.object(VM, 'from_environment', side_effect=self.verify), patch.object(VM.vm, 'start_vm') as start:
            with self.assertRaisesRegex(ValueError, 'differs from the active guest-launch lock'):
                VM.guest_run(config)
            start.assert_not_called()

    def test_guest_launch_rechecks_the_source_lock_after_staging(self):
        config = self.base / 'guest.json'
        write_json(config, dict(release=self.release))
        (self.checkout / 'ae/runners/fixture.py').write_text('changed while staging\n')
        with patch.object(VM, 'from_environment', side_effect=self.verify), patch.object(VM.vm, 'start_vm') as start:
            with self.assertRaisesRegex(ValueError, 'release source mismatch'):
                VM.guest_run(config)
            start.assert_not_called()

    def test_cli_imports_release_from_checkout_outside_repository_cwd(self):
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        for module in (VM, FANOUT):
            with self.subTest(module=module.__name__):
                result = subprocess.run([sys.executable, module.__file__, '--help'], cwd=self.base, env=environment,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
