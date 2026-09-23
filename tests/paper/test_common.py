import importlib.util
import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae'))
from repro.common import stats, configured_path, public_config
from repro.process import execute


class MeasurementTests(unittest.TestCase):
    def test_empty_invalid_measurements_are_errors(self):
        for values in ([], [True], [math.nan], [math.inf], [-1]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                stats(values)

    def test_event_weighted_statistics(self):
        self.assertEqual(stats([1, 1, 10])['mean'], 4)
        self.assertEqual(stats([1, 1, 10])['n'], 3)

    def test_config_paths_and_redaction(self):
        self.assertEqual(configured_path({'_config_dir': '/tmp', 'x': 'image'}, 'x'), Path('/tmp/image'))
        with self.assertRaises(ValueError):
            configured_path({}, 'kernel')
        self.assertEqual(public_config({'service': {'api_key': 'private', 'template': 'base'}}),
                         {'service': {'api_key': '<redacted>', 'template': 'base'}})

    def test_subprocess_failure_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'run'
            result = execute([sys.executable, '-c', 'raise SystemExit(7)'], out,
                             cwd=Path(tmp), timeout=5)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['returncode'], 7)
            with self.assertRaises(FileExistsError):
                execute([sys.executable, '-c', 'pass'], out, cwd=Path(tmp), timeout=5)


if __name__ == '__main__':
    unittest.main()
