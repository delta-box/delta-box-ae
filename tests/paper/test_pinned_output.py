"""Pinned measurement accepts prepared Cube contexts without reusing old runs."""
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

    def test_both_prepared_cube_contexts_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'table-02-cube'
            for name in ('control-plane', 'cube-memory'):
                context = path / name
                context.mkdir(parents=True)
                (context / 'before.json').write_text('{"owned": true}')
            self.assertEqual(prepare_output(path), path)
            self.assertFalse((path / 'environment.json').exists())
            for name in ('control-plane', 'cube-memory'):
                self.assertEqual((path / name / 'before.json').read_text(), '{"owned": true}')

    def test_prepared_contexts_do_not_allow_old_or_unknown_contents(self):
        for extra in ('environment.json', 'stale-result.json', 'unexpected-directory'):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'table-02-cube'
                (path / 'control-plane').mkdir(parents=True)
                (path / 'cube-memory').mkdir()
                if extra == 'unexpected-directory':
                    (path / extra).mkdir()
                else:
                    (path / extra).write_text('{}')
                with self.assertRaises(FileExistsError):
                    prepare_output(path)

    def test_prepared_context_symlinks_are_rejected(self):
        for name in ('control-plane', 'cube-memory'):
            for dangling in (False, True):
                with self.subTest(name=name, dangling=dangling), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'table-02-cube'
                    path.mkdir()
                    target = Path(directory) / 'other-context'
                    if not dangling:
                        target.mkdir()
                    (path / name).symlink_to(target, target_is_directory=True)
                    with self.assertRaises(FileExistsError):
                        prepare_output(path)

    def test_prepared_context_files_are_rejected(self):
        for name in ('control-plane', 'cube-memory'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'table-02-cube'
                path.mkdir()
                (path / name).write_text('{}')
                with self.assertRaises(FileExistsError):
                    prepare_output(path)

    def test_prepared_context_recovery_guards_are_rejected(self):
        for name in ('control-plane', 'cube-memory'):
            for guard_name in ('RECOVERY_REQUIRED.json', 'RECOVERY_REQUIRED'):
                with self.subTest(name=name, guard=guard_name), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'table-02-cube'
                    context = path / name
                    context.mkdir(parents=True)
                    (context / guard_name).write_text('{}')
                    with self.assertRaises(FileExistsError):
                        prepare_output(path)

    def test_dangling_recovery_guard_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'table-02-cube'
            context = path / 'control-plane'
            context.mkdir(parents=True)
            (context / 'RECOVERY_REQUIRED.json').symlink_to(context / 'missing')
            with self.assertRaises(FileExistsError):
                prepare_output(path)


if __name__ == '__main__':
    unittest.main()
