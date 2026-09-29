"""Source identity is provenance, never admission for the one-click pipeline."""
import contextlib
import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ae.scripts import run_review as review
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ae/runners'))
from ae.runners import vm_experiment
from ae.repro import figure09_reuse, cube_reuse
from ae.scripts.build_review_comparison import figure08_supplement
from tests.paper import test_oneclick_gpu as gpu_fixture

ROOT = Path(__file__).resolve().parents[2]
SOURCE = {'source_commit': 'a' * 40, 'source_sha256': 'b' * 64}
CHANGED = {'source_commit': 'c' * 40, 'source_sha256': 'd' * 64}

class SourceAdmissionTests(unittest.TestCase):
    def test_resume_records_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(review, 'current_source', return_value=SOURCE):
                original = review.Review(review.parser().parse_args([]), {}, root)
                original.save()
            old_bytes = (root / 'review.json').read_bytes()
            with patch.object(review, 'current_source', return_value=CHANGED):
                resumed = review.Review(review.parser().parse_args(['--resume', str(root)]), {}, root)
            self.assertEqual(resumed.record['release'], CHANGED)
            self.assertEqual(resumed.previous_record['release'], SOURCE)
            self.assertEqual(resumed.attempt, 'attempt-002')
            self.assertEqual((root / 'review.json').read_bytes(), old_bytes)

    def test_vm_guest_launch_accepts_changed_checkout(self):
        class ReachedRuntime(Exception):
            pass
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'run.json'
            config.write_text(json.dumps({'release': SOURCE}))
            with patch.object(vm_experiment, 'from_environment', return_value=CHANGED), \
                 patch.object(vm_experiment, 'runtime_directory', side_effect=ReachedRuntime):
                with self.assertRaises(ReachedRuntime):
                    vm_experiment.guest_run(config)

    def gpu_case(self):
        case = gpu_fixture.OneClickGPUTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_gpu_reports_a_completed_suite_from_a_different_source(self):
        fixture = self.gpu_case()
        row = fixture.run_gpu()
        suite = gpu_fixture.gpu.verify_suite(row['summary']['path'], CHANGED, list(gpu_fixture.protocol.BATCHES))
        self.assertEqual(suite['release'], gpu_fixture.SOURCE)

    def test_gpu_worker_source_change_does_not_discard_valid_timing(self):
        fixture = self.gpu_case()
        original = fixture.executor
        def changed(argv, output, **kwargs):
            result = original(argv, output, **kwargs)
            path = Path(argv[argv.index('--output') + 1])
            raw = json.loads(path.read_text())
            raw.update(worker_source_sha256='1' * 64, protocol_source_sha256='2' * 64)
            path.write_text(json.dumps(raw))
            return result
        fixture.executor = changed
        row = fixture.run_gpu()
        self.assertEqual((row['status'], row['successful_jobs']), ('ok', 8))

    def test_figure08_report_accepts_different_recorded_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'figure08.json'
            value = {'schema_version': 1, 'panels': [], 'release': SOURCE}
            path.write_text(json.dumps(value))
            self.assertEqual(figure08_supplement(path, expected_release=CHANGED), value)

    def test_uploaded_gpu_source_can_be_edited(self):
        from ae.scripts import figure08_remote
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'ae/runners/gpu_worker.py'
            path.parent.mkdir(parents=True)
            path.write_text('# edited worker\n')
            with patch.object(figure08_remote, 'ROOT', root):
                figure08_remote.verify_uploaded_source({'files': {'ae/runners/gpu_worker.py': '0' * 64}})

    def make_repository(self, root):
        from release.lock import PATHS
        for name in PATHS:
            path = root / name
            path = path if path.suffix else path / '.fixture'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# fixture\n')
        for name in ('ae/runners/vm_experiment.py', 'ae/runners/guest/war.py'):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# fixture\n')
        env = dict(os.environ, GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                   GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        for args in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'fixture']):
            subprocess.run(['git', '-C', str(root), *args], check=True, env=env, capture_output=True)
        return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()

    def test_reuse_accepts_modified_and_untracked_measurement_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            commit = self.make_repository(root)
            (root / 'ae/runners/vm_experiment.py').write_text('# changed fixture\n')
            (root / 'ae/runners/new_driver.py').write_text('# untracked fixture\n')
            self.assertTrue(figure09_reuse.measurement_fingerprint(root, commit))

    def test_cube_reuse_records_the_original_working_tree_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            commit = self.make_repository(root)
            original = dict(SOURCE, source_commit=commit)
            self.assertEqual(cube_reuse.source_commit_identity(root, original)['source_sha256'], original['source_sha256'])

if __name__ == '__main__':
    unittest.main()
