"""A complete paper bundle can be reused without writing shared input storage."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae'))
spec = importlib.util.spec_from_file_location('reproduce_prepare_fixture', ROOT / 'ae/reproduce.py')
reproduce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reproduce)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        scripts = self.root / 'scripts'
        scripts.mkdir()
        shutil.copy2(ROOT / 'ae/scripts/paper_data.py', scripts / 'paper_data.py')
        self.payload = b'cached fixture bytes\n'
        sha = hashlib.sha256(self.payload).hexdigest()
        self.object = self.root / 'traces/objects' / sha
        self.object.parent.mkdir(parents=True)
        self.object.write_bytes(self.payload)
        target = self.root / 'paper/test/data/trajectory.json'
        target.parent.mkdir(parents=True)
        target.symlink_to('../../../traces/objects/' + sha)
        (self.root / 'paper/test/files.jsonl').write_text(json.dumps(
            dict(target='paper/test/data/trajectory.json', sha256=sha, bytes=len(self.payload))) + '\n')

    def run_prepare(self, call):
        with patch.object(reproduce, 'AE_ROOT', self.root), \
             patch.object(reproduce.subprocess, 'call', side_effect=call), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return reproduce.prepare_inputs()

    def test_verified_readonly_objects_do_not_require_a_vendor_source_lock(self):
        self.object.chmod(0o444)
        self.object.parent.chmod(0o555)
        try:
            calls = []
            def call(argv):
                calls.append(argv)
                raise AssertionError('Verified data must not invoke import or vendor source verification')
            self.assertEqual(self.run_prepare(call), 0)
            self.assertEqual(len(calls), 0)
            self.assertEqual(list(self.object.parent.iterdir()), [self.object])
            self.assertEqual(self.object.read_bytes(), self.payload)
        finally:
            self.object.parent.chmod(0o755)

    def test_corrupt_objects_require_import_and_propagate_failure(self):
        self.object.write_bytes(b'corrupt')
        calls = []
        def call(argv):
            calls.append(argv)
            self.assertEqual(argv[-1], 'import')
            return 7
        self.assertEqual(self.run_prepare(call), 7)
        self.assertEqual(len(calls), 1)

    def test_successful_import_does_not_invoke_vendor_source_verification(self):
        self.object.write_bytes(b'corrupt')
        calls = []
        def call(argv):
            calls.append(argv)
            self.assertEqual(argv[-1], 'import')
            return 0
        self.assertEqual(self.run_prepare(call), 0)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
