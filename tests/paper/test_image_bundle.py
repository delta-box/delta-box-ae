"""Validate image bundle planning without root, Docker, or a KVM host."""
import contextlib


import hashlib


import importlib.util


import io


import json

import os

import subprocess

import sys


from pathlib import Path


import tempfile


import unittest


from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bundle = load('bundle_entry', 'ae/images/scripts/build_bundle.py')


class BundleTests(unittest.TestCase):

    def test_selected_image_inputs_are_checked_before_any_build(self):
        for case in ('matching', 'changed-master', 'changed-kernel', 'compile-kernel'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                master, kernel, key = root / 'source.xfs', root / 'selected-kernel', root / 'key.pub'
                master.write_bytes(b'image fixture')
                kernel.write_bytes(b'\x7fELFkernel fixture')
                key.write_text('ssh-ed25519 public-key')
                manifest = root / 'inputs.sha256'
                manifest.write_text(
                    hashlib.sha256(master.read_bytes()).hexdigest() + '  ubuntu-24.04.xfs\n' +
                    hashlib.sha256(kernel.read_bytes()).hexdigest() + '  vmlinux\n')
                kernel_flags = ['--kernel', str(kernel)]
                if case == 'changed-master':
                    master.write_bytes(b'changed image')
                elif case == 'changed-kernel':
                    kernel.write_bytes(b'\x7fELFchanged kernel')
                elif case == 'compile-kernel':
                    kernel.unlink()
                    source = root / 'linux'
                    (source / 'fs/overlayfs').mkdir(parents=True)
                    kernel_flags = ['--kernel-source', str(source)]
                flags = ['--master-xfs', str(master), *kernel_flags,
                         '--ssh-pubkey', str(key), '--output', str(root / 'new')]
                with patch.object(bundle, 'INPUT_CHECKSUMS', manifest), \
                     patch.object(bundle.sys, 'platform', 'linux'), \
                     patch.object(bundle.platform, 'machine', return_value='x86_64'), \
                     patch.object(bundle, 'run') as run, \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = bundle.main(flags)
                accepted = case in ('matching', 'compile-kernel')
                self.assertEqual(code, 0 if accepted else 1)
                self.assertEqual(run.call_count, 1 if accepted else 0)
                self.assertFalse((root / 'new').exists())


    def test_plan_does_not_scan_image_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master = root / 'source.xfs'
            master.write_bytes(b'plan fixture')
            args = self.inputs(root, ['--master-xfs', str(master), '--plan'])
            with patch.object(bundle, 'verify_image_inputs') as verify, \
                 patch.object(bundle, 'run') as run, \
                 patch.object(bundle, 'parser') as parser, contextlib.redirect_stdout(io.StringIO()):
                parser.return_value.parse_args.return_value = args
                self.assertEqual(bundle.main([]), 0)
            verify.assert_not_called()
            run.assert_not_called()
            self.assertFalse(args.output.exists())


    def test_documented_shell_plan_entrypoint_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kernel, master, key = root / 'vmlinux', root / 'master.xfs', root / 'key.pub'
            kernel.write_bytes(b'\x7fELFfixture')
            master.write_bytes(b'idle source fixture')
            key.write_text('ssh-ed25519 public-key-fixture')
            output = root / 'new-bundle'
            result = subprocess.run(['bash', str(ROOT / 'ae/build_images.sh'),
                                     '--master-xfs', str(master), '--kernel', str(kernel),
                                     '--groups', 'django', '--ssh-pubkey', str(key),
                                     '--output', str(output), '--plan'],
                                    env=dict(os.environ, AE_PYTHON=sys.executable),
                                    text=True, capture_output=True, check=True)
            plan = dict(json.loads(result.stdout)['steps'])
            self.assertEqual(list(plan), ['kernel', 'disks'])
            self.assertIn('--existing', plan['kernel'])
            self.assertIn('split', plan['disks'])
            self.assertEqual(plan['disks'][-2:], ['--groups', 'django'])
            self.assertFalse(output.exists())

    def test_passwordless_sudo_does_not_require_an_interactive_validation(self):
        with patch.object(bundle.os, 'geteuid', return_value=1000), \
             patch.object(bundle.subprocess, 'run') as run:
            run.return_value.returncode = 0
            bundle.authorize_disks()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], ['sudo', '-n', 'true'])

    def inputs(self, root, extra=()):
        kernel = root / 'linux'
        (kernel / 'fs/overlayfs').mkdir(parents=True)
        key = root / 'id_ed25519.pub'
        key.write_text('ssh-ed25519 test-public-key')
        return bundle.parser().parse_args(['--kernel-source', str(kernel), '--ssh-pubkey', str(key),
                                          '--output', str(root / 'bundle'), *extra])

    def test_plan_is_read_only_and_uses_selected_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.inputs(root, ['--source-image', 'ae/master:test'])
            bundle.validate(args)
            steps = dict(bundle.commands(args))
            self.assertFalse(args.output.exists())
            self.assertEqual(list(steps), ['kernel', 'disks'])
            self.assertIn('oci', steps['disks'])
            self.assertIn('ae/master:test', steps['disks'])
            args.source_image = None
            args.master_xfs = root / 'source.xfs'
            args.master_xfs.write_bytes(b'source')
            self.assertIn('split', dict(bundle.commands(args))['disks'])

    def test_existing_kernel_and_subset_do_not_schedule_kernel_compilation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / 'key.pub'
            key.write_text('ssh-ed25519 public-key')
            kernel = root / 'vmlinux'
            kernel.write_bytes(b'\x7fELFfixture')
            master = root / 'master.xfs'
            master.write_bytes(b'fixture')
            args = bundle.parser().parse_args([
                '--master-xfs', str(master), '--kernel', str(kernel),
                '--ssh-pubkey', str(key), '--output', str(root / 'new'),
                '--groups', 'django', 'django', '--plan'])
            bundle.validate(args)
            steps = dict(bundle.commands(args))
            self.assertEqual(steps['kernel'][-3:], ['--existing', str(kernel.resolve()), str(root / 'new/kernel')])
            self.assertEqual(steps['disks'][-2:], ['--groups', 'django'])
            self.assertFalse(args.output.exists())
            kernel.write_bytes(b'not-an-ELF')
            with self.assertRaisesRegex(ValueError, 'ELF vmlinux'):
                bundle.validate(args)

    def test_official_instance_image_is_not_treated_as_multi_environment_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.inputs(Path(tmp), ['--source-image',
                'swebench/sweb.eval.x86_64.django_1776_django-14997:latest'])
            with self.assertRaisesRegex(ValueError, 'per-instance'):
                bundle.validate(args)

    def test_checksum_and_existing_symlink_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installer = root / 'miniconda.sh'
            installer.write_bytes(b'installer')
            args = self.inputs(root, ['--miniconda', str(installer), '--miniconda-sha256', '0' * 64,
                                      '--image-tag', 'ae/master:test'])
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                bundle.validate(args)
            self.assertFalse(args.output.exists())
            args.miniconda_sha256 = hashlib.sha256(b'installer').hexdigest()
            bundle.validate(args)
            self.assertEqual([name for name, _ in bundle.commands(args)], ['master', 'kernel', 'disks'])
            args.output.symlink_to(root / 'absent')
            with self.assertRaisesRegex(ValueError, 'existing output'):
                bundle.validate(args)
            self.assertTrue(args.output.is_symlink())

    def test_failed_build_preserves_log_and_does_not_emit_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.inputs(Path(tmp), ['--source-image', 'ae/master:test'])
            bundle.validate(args)
            with patch.object(bundle, 'execute', return_value={'status': 'failed', 'returncode': 1}) as execute:
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                    bundle.run(args, bundle.commands(args))
                self.assertEqual(execute.call_count, 1)
            self.assertFalse((args.output / 'config.json').exists())
            self.assertEqual(json.loads((args.output / 'bundle.json').read_text())['status'], 'failed')

    def test_successful_build_connects_actual_outputs_to_runner_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.inputs(Path(tmp), ['--source-image', 'ae/master:test'])
            bundle.validate(args)

            def fake_execute(argv, output, **kwargs):
                names = (['kernel/vmlinux', 'kernel/artifacts.sha256'] if output.name == 'kernel' else
                         ['disks/base.xfs', 'disks/build.json', *[f'disks/data-{g}.xfs' for g in ('django', 'sympy', 'sci', 'tools')]])
                for name in names:
                    path = args.output / name
                    path.parent.mkdir(exist_ok=True)
                    path.write_text('test artifact')
                return {'status': 'ok', 'returncode': 0}

            with patch.object(bundle, 'execute', side_effect=fake_execute), \
                 patch.object(bundle, 'authorize_disks'), contextlib.redirect_stdout(io.StringIO()):
                bundle.run(args, bundle.commands(args))
            config = json.loads((args.output / 'config.json').read_text())
            self.assertTrue(Path(config['kernel']).is_file())
            self.assertTrue(Path(config['base_xfs']).is_file())
            self.assertEqual(config['payload'], '${AE_PAYLOAD}')
            self.assertEqual(config['measurement'], {'pin': False})


if __name__ == '__main__':
    unittest.main()
