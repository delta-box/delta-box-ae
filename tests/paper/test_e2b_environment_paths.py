"""Local paths must keep their configuration base after the Go working-dir change."""
import importlib.util
import copy
import hashlib
import json
import os
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


class E2BParentManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.storage = self.root / 'storage'
        self.infra = self.root / 'infra'
        self.infra.mkdir()
        archived = ROOT / 'ae/report/baseline-recheck-20260922/e2b-parent-isolation.json'
        # Keep the actual three-build graph and all 21 file paths. Small real
        # files replace the 1.9 GB byte payload, with independently computed hashes.
        self.manifest = json.loads(archived.read_text())
        self.manifest['destination'] = str(self.storage)
        for row in self.manifest['files']:
            path = self.storage / row['relative']
            path.parent.mkdir(parents=True, exist_ok=True)
            contents = row['relative'].encode()
            path.write_bytes(contents)
            row.update(bytes=len(contents), sha256=hashlib.sha256(contents).hexdigest())
        self.manifest['total_bytes'] = sum(row['bytes'] for row in self.manifest['files'])
        self.manifest['snapshot_bytes'] = sum(row['bytes'] for row in self.manifest['files']
                                              if row['relative'].startswith('templates/'))
        self.path = self.root / 'parents.json'
        self.config = {'_config_dir': str(self.root), 'e2b': {
            'execution': 'local', 'infra': 'infra', 'remote_path': '/usr/bin',
            'sidecar_ip': '127.0.0.1', 'storage': 'storage',
            'from_build': self.manifest['base_build'], 'parent_manifest': 'parents.json'}}
        self.parent = next(path for path in sorted((self.storage / 'templates').iterdir())
                           if path.name != self.manifest['base_build']) / 'memfile'

    def configure(self):
        self.path.write_text(json.dumps(self.manifest))
        with patch.object(environment.subprocess, 'check_output',
                          side_effect=lambda *args, **kwargs: '' if kwargs.get('text') else b''):
            return environment.configure(self.config, {})

    def test_actual_manifest_schema_and_parent_graph_verify_with_real_files(self):
        record = self.configure()
        self.assertEqual(len(record['snapshot_dependencies']), 21)
        self.assertEqual(len(record['base_build']), 6)
        self.assertEqual(record['parent_manifest']['path'], str(self.path))
        environment.verify_snapshot_inputs(record)

    def test_ancestor_tampering_rejected_before_and_after_execution(self):
        record = self.configure()
        contents = self.parent.read_bytes()
        # Same-size mutation ensures the SHA check, not merely size, rejects it.
        self.parent.write_bytes(bytes([contents[0] ^ 1]) + contents[1:])
        with self.assertRaisesRegex(ValueError, 'snapshot input changed'):
            environment.verify_snapshot_inputs(record)
        with self.assertRaisesRegex(ValueError, 'snapshot input changed'):
            self.configure()

    def test_wrong_base_and_storage_are_rejected(self):
        original = copy.deepcopy(self.manifest)
        self.manifest['base_build'] = self.parent.parent.name
        with self.assertRaisesRegex(ValueError, 'base/storage'):
            self.configure()
        self.manifest = original
        self.manifest['destination'] = str(self.root / 'unrelated-storage')
        with self.assertRaisesRegex(ValueError, 'base/storage'):
            self.configure()

    def test_unverified_or_incompatible_manifest_rejected(self):
        original = copy.deepcopy(self.manifest)
        for field, value in (('status', 'copying'), ('schema_version', 2),
                             ('independent_copies', 'true'), ('source_unchanged', False)):
            with self.subTest(field=field):
                self.manifest = {**original, field: value}
                with self.assertRaisesRegex(ValueError, 'base/storage'):
                    self.configure()

    def test_missing_ancestor_file_is_not_silently_removed_from_final_verification(self):
        self.manifest['files'] = [row for row in self.manifest['files']
                                  if row['relative'] != self.parent.relative_to(self.storage).as_posix()]
        with self.assertRaisesRegex(ValueError, 'required snapshot files'):
            self.configure()

    def test_missing_referenced_ancestor_header_rejected(self):
        del self.manifest['headers'][self.parent.parent.name]
        with self.assertRaisesRegex(ValueError, 'referenced ancestor'):
            self.configure()

    def test_duplicate_and_escaping_file_paths_rejected(self):
        original = copy.deepcopy(self.manifest)
        self.manifest['files'].append(copy.deepcopy(self.manifest['files'][0]))
        with self.assertRaisesRegex(ValueError, 'repeats'):
            self.configure()
        self.manifest = original
        self.manifest['files'][0]['relative'] = '../outside'
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.configure()

    def test_same_bytes_do_not_hide_symlink_replacement_after_execution(self):
        record = self.configure()
        target = self.root / 'shared-parent'
        target.write_bytes(self.parent.read_bytes())
        self.parent.unlink()
        self.parent.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'independent file'):
            environment.verify_snapshot_inputs(record)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.configure()

    def test_same_bytes_do_not_hide_hardlink_to_historical_storage(self):
        target = self.root / 'shared-parent'
        target.write_bytes(self.parent.read_bytes())
        self.parent.unlink()
        os.link(target, self.parent)
        with self.assertRaisesRegex(ValueError, 'independent file'):
            self.configure()

    def test_new_child_build_is_not_part_of_immutable_inputs(self):
        record = self.configure()
        child = self.storage / 'templates/new-child/memfile'
        child.parent.mkdir()
        child.write_bytes(b'new child')
        environment.verify_snapshot_inputs(record)
        child.write_bytes(b'child changes are outside the parent-input check')
        environment.verify_snapshot_inputs(record)


if __name__ == '__main__':
    unittest.main()
