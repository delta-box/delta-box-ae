"""Check public installer download and build entry wiring without Docker/KVM."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import fetch_miniconda

SCRIPTS = Path(__file__).resolve().parent
DATA = b'installer fixture, never executed'
DIGEST = hashlib.sha256(DATA).hexdigest()


class DownloadTests(unittest.TestCase):
    def test_verified_bytes_are_published_without_replacing_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'installer.sh'
            with patch.object(fetch_miniconda, 'SHA256', DIGEST), \
                 patch.object(fetch_miniconda, 'urlopen', return_value=io.BytesIO(DATA)):
                self.assertEqual(fetch_miniconda.fetch(output), DIGEST)
            self.assertEqual(output.read_bytes(), DATA)
            with patch.object(fetch_miniconda, 'urlopen') as network:
                with self.assertRaises(FileExistsError):
                    fetch_miniconda.fetch(output)
                network.assert_not_called()
            self.assertEqual(output.read_bytes(), DATA)

    def test_bad_download_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'installer.sh'
            with patch.object(fetch_miniconda, 'SHA256', DIGEST), \
                 patch.object(fetch_miniconda, 'urlopen', return_value=io.BytesIO(b'bad bytes')):
                with self.assertRaises(ValueError):
                    fetch_miniconda.fetch(output)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_interrupted_transfer_removes_partial_download(self):
        class Interrupted(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError('connection interrupted')
                return super().read(size)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(fetch_miniconda, 'urlopen', return_value=Interrupted(DATA)):
                with self.assertRaises(OSError):
                    fetch_miniconda.fetch(Path(tmp) / 'installer.sh')
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def exercise_master(self, mode, *, fail_download=False, existing_image=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts, historical, binaries = root / 'scripts', root / 'historical', root / 'bin'
            for folder in (scripts, historical, binaries):
                folder.mkdir()
            shutil.copy2(SCRIPTS / 'build_master.sh', scripts / 'build_master.sh')
            shutil.copy2(SCRIPTS.parent / 'Dockerfile.master', root / 'Dockerfile.master')
            (historical / 'env_specs.json').write_text('{}')
            (historical / 'install_envs.py').write_text('# fixture')
            (scripts / 'install_envs_strict.py').write_text('# fixture')
            (scripts / 'fetch_miniconda.py').write_text(
                'from pathlib import Path\nimport sys\n' +
                ('raise SystemExit(1)\n' if fail_download else
                 f'Path(sys.argv[1]).write_bytes({DATA!r})\nprint({DIGEST!r})\n'))
            docker = binaries / 'docker'
            docker.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['DOCKER_CALLS'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
if args[:2] == ['image', 'inspect']:
    if '--format' in args:
        print('fixture-image-id')
    else:
        raise SystemExit(int(os.environ['IMAGE_EXISTS'] != '1'))
elif args and args[0] == 'build':
    assert (Path(args[-1]) / 'miniconda.sh').is_file()
''')
            docker.chmod(0o755)
            calls_path, output = root / 'docker-calls.jsonl', root / 'work'
            env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                       DOCKER_CALLS=str(calls_path), IMAGE_EXISTS=str(int(existing_image)),
                       UBUNTU_IMAGE='ubuntu:24.04')
            if mode == 'download':
                args = ['--from-ubuntu', str(output), 'ae/master:test']
            else:
                installer = root / 'local-installer.sh'
                installer.write_bytes(DATA)
                args = [str(installer), DIGEST, str(output), 'ae/master:test']
            result = subprocess.run(['bash', str(scripts / 'build_master.sh'), *args],
                                    env=env, text=True, capture_output=True)
            calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
            return result, calls, output.exists(), (output / 'image-id.txt').exists()

    def test_download_and_existing_installer_modes_feed_the_same_build_recipe(self):
        for mode in ('download', 'existing'):
            with self.subTest(mode=mode):
                result, calls, _, completed = self.exercise_master(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                build = next(args for args in calls if args[0] == 'build')
                self.assertIn('MINICONDA_SHA256=' + DIGEST, build)
                self.assertIn('UBUNTU_IMAGE=ubuntu:24.04', build)
                self.assertTrue(completed)

    def test_download_failure_prevents_docker_build(self):
        result, calls, _, completed = self.exercise_master('download', fail_download=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(args[0] == 'build' for args in calls))
        self.assertFalse(completed)

    def test_existing_image_is_not_overwritten(self):
        result, calls, created, _ = self.exercise_master('download', existing_image=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(created)
        self.assertFalse(any(args[0] == 'build' for args in calls))


if __name__ == '__main__':
    unittest.main()
