import json
import importlib.util
import tarfile
from types import SimpleNamespace
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ae'))
from repro.repositories import LocalObjectReadError, offline_git, local_type, read_blob, select_repository


class RepositorySourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / 'repos/swe-bench_owner__project-1'
        self.repo.mkdir(parents=True)
        self.git('init', '-q')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/example.py').write_text('original\n')
        self.git('add', '.')
        self.git('-c', 'user.name=AE Test', '-c', 'user.email=ae@example.invalid', 'commit', '-qm', 'Recorded tree')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        (self.repo / 'src/example.py').write_text('working tree changed\n')
        self.config = {'payload': str(self.root), 'repository_fallback': 'same-project-commit'}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True, stderr=subprocess.PIPE)

    def test_uses_exact_commit_in_same_project_clone(self):
        found = select_repository(self.config, 'owner__project-999', self.commit, ['src/example.py'])
        self.assertEqual(found, self.repo)
        self.assertEqual(self.git('show', self.commit + ':src/example.py'), 'original\n')

    def test_default_does_not_expand_clone_selection(self):
        with self.assertRaises(FileNotFoundError):
            select_repository({'payload':str(self.root)}, 'owner__project-999', self.commit)

    def test_missing_commit_file_and_cross_project_are_rejected(self):
        for instance, commit, files in [('owner__project-999', '0'*40, []),
                                        ('owner__project-999', self.commit, ['missing.py']),
                                        ('other__project-999', self.commit, ['src/example.py']),
                                        ('owner__project-999', self.commit, ['src'])]:
            with self.subTest(instance=instance, commit=commit, files=files), self.assertRaises(FileNotFoundError):
                select_repository(self.config, instance, commit, files)

    def promisor(self):
        marker = self.root / 'network-helper-entered'
        helper = self.root / 'remote-helper.sh'
        helper.write_text('#!/bin/sh\nprintf entered > "' + str(marker) + '"\nsleep 60\n')
        helper.chmod(0o755)
        self.git('config', 'core.repositoryformatversion', '1')
        self.git('config', 'extensions.partialclone', 'origin')
        self.git('config', 'remote.origin.url', 'ext::' + str(helper))
        self.git('config', 'remote.origin.promisor', 'true')
        self.git('config', 'protocol.ext.allow', 'always')
        return marker

    def test_missing_promisor_commit_never_starts_remote_transport(self):
        marker = self.promisor()
        # Even a permissive caller/local repo cannot override the empty whitelist.
        before = (self.repo / '.git/config').read_bytes()
        with patch.dict(os.environ, {'GIT_ALLOW_PROTOCOL': 'ext'}):
            with self.assertRaisesRegex(FileNotFoundError, 'cached locally'):
                select_repository(self.config, 'owner__project-1', '0' * 40)
        self.assertFalse(marker.exists())
        self.assertEqual((self.repo / '.git/config').read_bytes(), before)

    def test_present_tree_with_missing_promisor_blob_is_not_enough(self):
        blob = self.git('rev-parse', self.commit + ':src/example.py').strip()
        (self.repo / '.git/objects' / blob[:2] / blob[2:]).unlink()
        marker = self.promisor()
        metadata = self.git('ls-tree', self.commit, 'src/example.py')
        self.assertIn(blob, metadata)  # Recorded tree still proves only the path/OID.
        with self.assertRaisesRegex(FileNotFoundError, 'required blobs cached locally'):
            select_repository(self.config, 'owner__project-1', self.commit, ['src/example.py'])
        with self.assertRaisesRegex(LocalObjectReadError, 'not locally readable'):
            read_blob(self.repo, self.commit, 'src/example.py')
        self.assertFalse(marker.exists())

    def test_cached_promisor_blob_reads_exact_commit_without_worktree_or_replace(self):
        marker = self.promisor()
        blob = self.git('rev-parse', self.commit + ':src/example.py').strip()
        replacement = subprocess.check_output(['git', '-C', str(self.repo), 'hash-object', '-w', '--stdin'], input=b'replacement\n').decode().strip()
        self.git('replace', blob, replacement)
        with patch.dict(os.environ, {'GIT_DIR': str(self.root / 'wrong')}):
            self.assertEqual(read_blob(self.repo, self.commit, 'src/example.py'), b'original\n')
        self.assertFalse(marker.exists())

    def test_timeout_kills_owned_git_descendants_not_other_processes(self):
        executable = self.root / 'bin'
        executable.mkdir()
        child_pid = self.root / 'owned-child.pid'
        fake_git = executable / 'git'
        fake_git.write_text('#!' + sys.executable + '\nimport os, time\npid=os.fork()\nif pid==0:\n time.sleep(60)\nelse:\n open(' + repr(str(child_pid)) + ', "w").write(str(pid))\n time.sleep(60)\n')
        fake_git.chmod(0o755)
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            with patch.dict(os.environ, {'PATH': str(executable) + os.pathsep + os.environ['PATH']}):
                with self.assertRaisesRegex(LocalObjectReadError, 'timed out'):
                    offline_git(self.repo, 'cat-file', '-t', self.commit, timeout=2)
            pid = int(child_pid.read_text())
            # A dead orphan can remain a zombie briefly until init reaps it.
            status = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], stdout=subprocess.PIPE, text=True).stdout.strip()
            self.assertTrue(not status or status.startswith('Z'), status)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait()

    @staticmethod
    def process_exists(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_abnormal_git_failure_is_not_reported_as_missing_data(self):
        result = subprocess.CompletedProcess(['git'], 128, b'', b'fatal: detected dubious ownership in repository')
        with patch('repro.repositories.offline_git', return_value=result):
            with self.assertRaisesRegex(LocalObjectReadError, 'dubious ownership'):
                local_type(self.repo, self.commit)

    def test_war_lower_archive_uses_offline_blob_reader(self):
        source = Path(__file__).resolve().parents[2] / 'ae/runners/vm_experiment.py'
        spec = importlib.util.spec_from_file_location('war_offline_fixture', source)
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'path', [str(source.parent), *sys.path]):
            spec.loader.exec_module(module)
        config = self.root / 'config.json'
        config.write_text(json.dumps(self.config))
        actions = self.root / 'actions.json'
        actions.write_text(json.dumps(dict(instance_id='owner__project-999', base_commit=self.commit, edits=[{'file_path': 'src/example.py'}])))
        out = self.root / 'out'
        out.mkdir()
        args = SimpleNamespace(experiment='figure-09', forks=None, arm='ext4', input_key='fixture', actions=actions, config=config)
        with patch.object(module, 'read_blob', wraps=read_blob) as reader:
            _, record = module.build_extra(args, out)
        reader.assert_called_once_with(self.repo, self.commit, 'src/example.py')
        with tarfile.open(out / 'lower.tar') as tar:
            self.assertEqual(tar.extractfile('src/example.py').read(), b'original\n')
        self.assertIn('fetch transports disabled', record['repository_source']['selection'])

    def test_primary_is_preferred_and_inputs_cannot_escape(self):
        self.assertEqual(select_repository(self.config, 'owner__project-1', self.commit), self.repo)
        with self.assertRaises(ValueError):
            select_repository(self.config, '../owner__project-1', self.commit)
        with self.assertRaises(ValueError):
            select_repository(self.config, 'owner__project-1', self.commit, ['../other'])


if __name__ == '__main__':
    unittest.main()
