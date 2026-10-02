"""Configured dependency policy reaches the real staging function without workers."""
import ast
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def staging_function():
    tree = ast.parse((ROOT / 'ae/runners/baseline.py').read_text())
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'stage_local_dependencies')
    namespace = dict(Path=Path, shutil=shutil,
        configured_path=lambda config, key, required=True: Path(config[key]) if config.get(key) else None,
        file_record=lambda path: {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'baseline.py', 'exec'), namespace)
    return namespace['stage_local_dependencies']


class DependencyPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.venv = self.root / 'venv'
        module = self.venv / 'lib/python3.11/site-packages/litellm'
        module.mkdir(parents=True)
        (module / '__init__.py').write_text('# fixture library identity\n')
        (module / 'model_prices_and_context_window_backup.json').write_text('{}')
        self.nltk = self.root / 'nltk'
        for name in ('tokenizers/punkt/english.pickle', 'corpora/stopwords/english',
                     'tokenizers/punkt_tab/english/abbrev_types.txt', 'tokenizers/punkt_tab/english/collocations.tab',
                     'tokenizers/punkt_tab/english/ortho_context.tab', 'tokenizers/punkt_tab/english/sent_starters.txt'):
            path = self.nltk / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture dependency bytes')
        self.config = {'moatless_venv': str(self.venv), 'nltk_data': str(self.nltk)}

    def test_default_local_policy_retains_offline_nltk_and_model_metadata(self):
        env = {}
        record = staging_function()(self.config, self.root / 'output', env)
        self.assertEqual(env['LITELLM_LOCAL_MODEL_COST_MAP'], 'True')
        self.assertEqual(env['NLTK_DATA'], str(self.root / 'output/nltk_data'))
        self.assertNotIn('BASELINE_LOG_LEVEL', env)
        self.assertEqual(record['litellm_cost_map'], 'local')
        self.assertEqual(record['baseline_log_level'], 'driver-default')
        self.assertEqual(len(record['nltk_data']['files']), 6)

    def test_remote_and_info_override_existing_environment_without_losing_nltk(self):
        config = {**self.config, 'litellm_cost_map': 'remote', 'baseline_log_level': 'INFO'}
        env = {'LITELLM_LOCAL_MODEL_COST_MAP': 'True', 'BASELINE_LOG_LEVEL': 'WARNING', 'NLTK_DATA': '/old'}
        record = staging_function()(config, self.root / 'output', env)
        self.assertNotIn('LITELLM_LOCAL_MODEL_COST_MAP', env)
        self.assertEqual(env['BASELINE_LOG_LEVEL'], 'INFO')
        self.assertEqual(env['NLTK_DATA'], str(self.root / 'output/nltk_data'))
        self.assertEqual(record['litellm_cost_map'], 'remote')
        self.assertEqual(record['baseline_log_level'], 'INFO')

    def test_invalid_policy_or_log_level_rejects_the_plan(self):
        for index, (key, value) in enumerate([('litellm_cost_map', 'unknown'), ('baseline_log_level', 'TRACE')]):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                staging_function()({**self.config, key: value}, self.root / str(index), {})


if __name__ == '__main__':
    unittest.main()
