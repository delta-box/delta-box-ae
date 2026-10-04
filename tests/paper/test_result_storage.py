"""Exercise real result backup, failure preservation, and run exclusion."""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ae.repro import result_storage as storage
from ae.scripts import run_review as review


class ResultStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.results = self.base / 'results'
        self.results.mkdir()
        self.backups = self.base / 'backups'
        self.lock = self.base / 'work/results.lock'
        self.old = self.results / 'old/run.json'
        self.old.parent.mkdir()
        self.old.write_text('{"status":"failed","source_commit":"old"}\n')
        self.old.chmod(0o640)

    def rotate(self):
        with storage.run_lock(self.lock):
            return storage.prepare_latest(self.results, self.backups)

    def test_real_open_file_prevents_backup(self):
        # A legacy producer can exist without the new run lock.
        with self.old.open('rb'):
            with self.assertRaisesRegex(ValueError, 'active references'):
                self.rotate()
        self.assertTrue(self.old.exists())
        self.assertFalse(self.backups.exists())

    def test_real_mapped_file_prevents_backup(self):
        import mmap
        with self.old.open('rb') as stream:
            memory = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            with self.assertRaisesRegex(ValueError, 'active references'):
                self.rotate()
        finally:
            memory.close()
        self.assertTrue(self.old.exists())

    def test_mountinfo_error_is_ignored_only_for_confirmed_exit(self):
        proc = Path('/proc') / str(os.getpid())
        original_open, original_read = Path.open, Path.read_text
        original_iter = Path.iterdir
        def iter_proc(path):
            return iter([proc]) if path == Path('/proc') else original_iter(path)
        for failure, state, accepted in (
            (errno.EINVAL, 'State:\tZ (zombie)\n', True),
            (errno.EINVAL, FileNotFoundError(), True),
            (errno.EINVAL, 'State:\tS (sleeping)\n', False),
            (errno.EINVAL, PermissionError(), False),
            (errno.EIO, 'State:\tZ (zombie)\n', False),
        ):
            with self.subTest(errno=failure, state=repr(state)):
                def open_proc(path, *args, **kwargs):
                    if path == proc / 'mountinfo':
                        raise OSError(failure, 'injected mountinfo error', str(path))
                    return original_open(path, *args, **kwargs)
                def read_proc(path, *args, **kwargs):
                    if path == proc / 'status':
                        if isinstance(state, BaseException):
                            raise state
                        return state
                    return original_read(path, *args, **kwargs)
                with patch.object(Path, 'open', open_proc), \
                     patch.object(Path, 'read_text', read_proc), \
                     patch.object(Path, 'iterdir', iter_proc):
                    if accepted:
                        storage.require_idle(self.results)
                    else:
                        with self.assertRaises(OSError):
                            storage.require_idle(self.results)

    def test_lock_is_shared_across_invocations(self):
        with storage.run_lock(self.lock):
            with self.assertRaisesRegex(ValueError, 'Another AE'):
                with storage.run_lock(self.lock):
                    self.fail('second invocation acquired the active lock')
        with storage.run_lock(self.lock):
            pass

    def test_symlink_or_overlapping_backup_is_rejected(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.backups, target_is_directory=True)
        for destination in (alias, self.results / 'archive', self.base):
            with self.subTest(destination=destination):
                with self.assertRaises(ValueError):
                    storage.prepare_latest(self.results, destination)
        self.assertTrue(self.old.exists())

    def test_incomplete_retirement_requires_inspection(self):
        (self.base / '.results-retired-previous').mkdir()
        with self.assertRaisesRegex(ValueError, 'interrupted results replacement'):
            self.rotate()
        self.assertTrue(self.old.exists())

class ReviewStorageIntegrationTests(unittest.TestCase):
    def test_two_complete_invocations_rotate_at_stable_path_without_experiments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / 'config.json'
            config.write_text('{}')
            class FakeReview:
                def __init__(self, args, config, output):
                    self.output = output
                    self.record = {'status':'ok', 'release':{'source_commit':'current'}}
                def print_summary(self):
                    pass
                def run(self):
                    (self.output / 'review.json').write_text(json.dumps(self.record))
                    (self.output / 'SUMMARY.md').write_text('test fixture')
                    return 0
            # Non-root callers cannot inspect their own sshd session in /proc.
            with patch.object(review, 'REPO', root), patch.object(review, 'Review', FakeReview), \
                 patch('repro.result_storage.active_references', return_value=[]), \
                 patch.object(review, 'check_timeout'), patch.object(review, 'load_config', return_value={}), \
                 patch.dict(os.environ, {'AE_RESULTS_BACKUP_ROOT':str(root / 'backup')}), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.main(['--config',str(config)]), 0)
                first = (root / 'ae/results/review.json').read_bytes()
                self.assertEqual(review.main(['--config',str(config)]), 0)
            archives = list((root / 'backup').glob('results-*'))
            self.assertEqual(len(archives), 1)
            self.assertEqual((archives[0] / 'results/review.json').read_bytes(), first)
            new = json.loads((root / 'ae/results/review.json').read_text())
            self.assertEqual(new['previous_results_backup']['path'], str(archives[0]))
            self.assertEqual(new['release']['source_commit'], 'current')

    def test_explicit_output_never_rotates_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            output = root / 'explicit'
            output.mkdir()
            (output / 'keep').write_text('old result')
            with patch.object(review, 'REPO', root), patch.object(review, 'Review') as runner, \
                 patch.object(review, 'check_timeout'), patch.object(review, 'load_config', return_value={}), \
                 patch.object(review, 'prepare_latest') as rotate, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(review.main(['--output',str(output)]), 2)
            rotate.assert_not_called()
            runner.return_value.run.assert_not_called()
            self.assertEqual((output / 'keep').read_text(), 'old result')


if __name__ == '__main__':
    unittest.main()
