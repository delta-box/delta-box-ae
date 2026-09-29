"""Pinned measurement output may already contain the Cube memory directory."""
import tempfile
import unittest
from pathlib import Path

from ae.scripts.run_pinned_measurement import prepare_output


class PinnedOutputTests(unittest.TestCase):
    def test_fresh_directory_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'table-02-cube'
            self.assertEqual(prepare_output(path), path)
            self.assertTrue(path.is_dir())

    def test_cube_memory_parent_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'table-02-cube'
            (path / 'cube-memory').mkdir(parents=True)
            self.assertEqual(prepare_output(path), path)

    def test_existing_environment_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'table-02-cube'
            path.mkdir()
            (path / 'environment.json').write_text('{}')
            with self.assertRaises(FileExistsError):
                prepare_output(path)


if __name__ == '__main__':
    unittest.main()
