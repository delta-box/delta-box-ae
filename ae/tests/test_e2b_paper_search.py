import ast
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / 'ae/scripts/e2b_paper_search.py'
SOURCE = 'import os, subprocess, logging\nfrom typing import Optional, List\nlogger=logging.getLogger(__name__)\nclass FileRepository:\n    marker = 17\n    def find_exact_matches(self, search_text, file_pattern=None):\n        return []\n'

class PaperSearchTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(ADAPTER.is_file(), 'Paper search adapter must preserve the recovered directory-search semantics')
        spec=importlib.util.spec_from_file_location('paper_search_test',ADAPTER)
        self.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.mod)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.source=self.base/'shared';self.payload=self.base/'job/payload'
        (self.source/'moatless/repository').mkdir(parents=True);self.payload.mkdir(parents=True)
        (self.source/'moatless/repository/file.py').write_text(SOURCE)
        (self.payload/'moatless-det-src').symlink_to(self.source,target_is_directory=True)
        self.repo=self.base/'repo';(self.repo/'src').mkdir(parents=True)
        (self.repo/'src/handlers.py').write_text('prefix\nclass ASGIStaticFilesHandler: pass\n')
        self.before=hashlib.sha256((self.source/'moatless/repository/file.py').read_bytes()).hexdigest()
    def stage(self):
        proof=self.mod.stage_historical_search(self.payload)
        path=self.payload/'moatless-det-src/moatless/repository/file.py'
        ns={'__name__':'staged_paper_search'};exec(compile(path.read_text(),str(path),'exec'),ns)
        instance=ns['FileRepository']();instance.repo_path=str(self.repo)
        return instance,proof,path
    def test_empty_wildcard_match_keeps_original_directory_scope(self):
        instance,proof,path=self.stage()
        self.assertEqual(instance.find_exact_matches('ASGIStaticFilesHandler','**/staticfiles/*test*.py'),[('src/handlers.py',2)])
    def test_directory_prefix_bounds_the_legacy_search(self):
        (self.repo/'outside.py').write_text('ASGIStaticFilesHandler\n')
        instance,_,_=self.stage()
        self.assertEqual(instance.find_exact_matches('ASGIStaticFilesHandler','src/**/test*.py'),[('src/handlers.py',2)])
    def test_explicit_file_and_no_match(self):
        instance,_,_=self.stage()
        self.assertEqual(instance.find_exact_matches('ASGIStaticFilesHandler','src/handlers.py'),[('src/handlers.py',2)])
        self.assertEqual(instance.find_exact_matches('absent-value','src/handlers.py'),[])
    def test_staging_does_not_mutate_shared_source(self):
        instance,proof,path=self.stage()
        self.assertFalse((self.payload/'moatless-det-src').is_symlink())
        self.assertEqual(hashlib.sha256((self.source/'moatless/repository/file.py').read_bytes()).hexdigest(),self.before)
        self.assertEqual(instance.marker,17)
        self.assertEqual(proof['before_sha256'],self.before)
        self.assertEqual(proof['after_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(proof['historical_function_sha256'],'44143c2e8634ff331beeb2c5e60b404e8cdecca42b5a3479f8e3188642c4db9c')
    def test_match_order_is_independent_of_native_grep_traversal(self):
        from unittest.mock import patch
        instance,_,_=self.stage()
        for name in ('z.py','a.py'):(self.repo/name).write_text('token\n')
        reply=types.SimpleNamespace(returncode=0,stdout='z.py:1:token\na.py:1:token\nz.py:1:token\n',stderr='')
        with patch('subprocess.run',return_value=reply):
            matches=instance.find_exact_matches('token')
        self.assertEqual(matches,[('a.py',1),('z.py',1),('z.py',1)])

    def test_parent_files_precede_sorted_subdirectories(self):
        from unittest.mock import patch
        instance,_,_=self.stage()
        reply=types.SimpleNamespace(returncode=0,stdout='doc/api/a.rst:1:token\ndoc/conf.py:2:token\n',stderr='')
        with patch('subprocess.run',return_value=reply):
            matches=instance.find_exact_matches('token')
        self.assertEqual(matches,[('doc/conf.py',2),('doc/api/a.rst',1)])

    def test_rejects_missing_repository_method(self):
        (self.source/'moatless/repository/file.py').write_text('class Different: pass\n')
        with self.assertRaises(ValueError):self.mod.stage_historical_search(self.payload)

if __name__ == '__main__':unittest.main()
