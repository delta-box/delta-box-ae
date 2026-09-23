import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('baseline_dependencies_test_runner', ROOT / 'ae/runners/baseline.py')
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


class BaselineDependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.venv = self.root / 'venv'
        litellm = self.venv / 'lib/python3.11/site-packages/litellm'
        litellm.mkdir(parents=True)
        (litellm / 'model_prices_and_context_window_backup.json').write_text('{"test-model": {}}')
        (litellm / '__init__.py').write_text('# installed source evidence\n')
        self.cache = self.root / 'cache'
        for name in ('tokenizers/punkt/english.pickle', 'corpora/stopwords/english',
                     'tokenizers/punkt_tab/english/abbrev_types.txt',
                     'tokenizers/punkt_tab/english/collocations.tab',
                     'tokenizers/punkt_tab/english/ortho_context.tab',
                     'tokenizers/punkt_tab/english/sent_starters.txt'):
            path = self.cache / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'nonempty resource fixture')
        self.config = {'moatless_venv': str(self.venv), 'nltk_data': str(self.cache)}
        self.out = self.root / 'run'
        self.out.mkdir()

    def test_real_resource_files_are_copied_and_hashed_with_explicit_environment(self):
        env = {}
        record = baseline.stage_local_dependencies(self.config, self.out, env)
        self.assertEqual(env['NLTK_DATA'], str(self.out / 'nltk_data'))
        self.assertEqual(env['LITELLM_LOCAL_MODEL_COST_MAP'], 'True')
        self.assertEqual(len(record['nltk_data']['files']), 6)
        for source in self.cache.rglob('*'):
            if source.is_file():
                staged = self.out / 'nltk_data' / source.relative_to(self.cache)
                self.assertEqual(source.read_bytes(), staged.read_bytes())
                self.assertFalse(staged.is_symlink())

    def test_empty_legacy_marker_directory_is_rejected(self):
        (self.cache / 'tokenizers/punkt/english.pickle').unlink()
        with self.assertRaisesRegex(ValueError, 'Incomplete offline NLTK'):
            baseline.stage_local_dependencies(self.config, self.out, {})
        self.assertFalse((self.out / 'nltk_data').exists())

    def test_private_criu_selection_is_explicit_and_hashed(self):
        binary = self.root / 'private-criu'
        binary.write_bytes(b'private executable fixture')
        env = {'FINALBENCH_CRIU': '/usr/sbin/criu'}
        with mock.patch.object(baseline.subprocess, 'check_output', return_value='Version: 4.2\n') as version:
            record = baseline.select_criu_binary({'criu_bin': str(binary)}, env)
        version.assert_called_once_with([str(binary), '--version'], text=True, timeout=10)
        self.assertEqual(env['FINALBENCH_CRIU'], str(binary))
        self.assertEqual(record['selection'], 'config.criu_bin')
        self.assertEqual(record['version'], 'Version: 4.2')
        self.assertTrue(record['sha256'])

    def test_unconfigured_criu_keeps_existing_environment_choice(self):
        binary = self.root / 'existing-criu'
        binary.write_bytes(b'existing executable fixture')
        env = {'FINALBENCH_CRIU': str(binary)}
        with mock.patch.object(baseline.subprocess, 'check_output', return_value='Version: 3.16.1\n'):
            record = baseline.select_criu_binary({}, env)
        self.assertEqual(env['FINALBENCH_CRIU'], str(binary))
        self.assertEqual(record['selection'], 'historical FINALBENCH_CRIU/PATH default')

    def test_environment_command_is_resolved_in_supplied_path(self):
        binary = self.root / 'criu'
        binary.write_bytes(b'existing executable fixture')
        binary.chmod(0o755)
        env = {'FINALBENCH_CRIU': 'criu', 'PATH': str(self.root)}
        with mock.patch.object(baseline.subprocess, 'check_output', return_value='Version: 3.16.1\n') as version:
            baseline.select_criu_binary({}, env)
        version.assert_called_once_with([str(binary), '--version'], text=True, timeout=10)
        self.assertEqual(env['FINALBENCH_CRIU'], str(binary))

    def test_unknown_environment_command_is_not_replaced_by_default(self):
        with self.assertRaisesRegex(FileNotFoundError, 'FINALBENCH_CRIU command not found'):
            baseline.select_criu_binary({}, {'FINALBENCH_CRIU': 'missing-criu', 'PATH': str(self.root)})


if __name__ == '__main__':
    unittest.main()
