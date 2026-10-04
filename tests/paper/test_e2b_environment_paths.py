"""Local paths must keep their configuration base after the Go working-dir change."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae'))
spec = importlib.util.spec_from_file_location('e2b_paths', ROOT / 'ae/runners/e2b_environment.py')
environment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(environment)


class E2BEnvironmentPathTests(unittest.TestCase):
    def test_local_paths_resolve_against_config_not_infra_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            build = root / 'storage/templates/base'
            build.mkdir(parents=True)
            for name in ('metadata.json', 'snapfile', 'memfile', 'memfile.header', 'rootfs.ext4', 'rootfs.ext4.header'):
                (build / name).write_bytes(b'fixture')
            binary = root / 'bin/resume'
            binary.parent.mkdir()
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o700)
            (root / 'infra').mkdir()
            config = {'_config_dir': str(root), 'e2b': {
                'execution': 'local', 'infra': 'infra', 'remote_path': '/usr/bin',
                'sidecar_ip': '127.0.0.1', 'storage': 'storage', 'from_build': 'base',
                'resume_binary': 'bin/resume', 'sandbox_dir': 'sandbox',
                'gocache': 'cache', 'gomodcache': 'modules'}}
            env = {'E2B_L1_KEY': 'stale-key'}
            with patch.object(environment.subprocess, 'check_output',
                              side_effect=lambda *args, **kwargs: '' if kwargs.get('text') else b''):
                record = environment.configure(config, env)
            self.assertEqual(record['storage'], str(root / 'storage'))
            self.assertEqual(record['resume_binary']['path'], env['E2B_RESUME_BINARY'])
            for key, relative in [('E2B_RESUME_BINARY', 'bin/resume'), ('E2B_SANDBOX_DIR', 'sandbox'),
                                  ('E2B_GOCACHE', 'cache'), ('E2B_GOMODCACHE', 'modules')]:
                self.assertEqual(env[key], str(root / relative))
            self.assertNotIn('E2B_L1_KEY', env)
            self.assertEqual(len(record['base_build']), 6)


if __name__ == '__main__':
    unittest.main()
