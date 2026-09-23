import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('cube_environment', ROOT / 'ae/runners/cube_environment.py')
cube = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cube)


class CubeEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sdk = self.root / 'sdk'
        (self.sdk / 'cubesandbox').mkdir(parents=True)
        (self.sdk / 'cubesandbox/__init__.py').write_text('# SDK fixture\n')
        self.binary = self.root / 'cubelet'
        self.binary.write_bytes(b'cubelet fixture')
        self.detail = {
            'templateID': 'canonical', 'status': 'READY', 'version': 'v2',
            'createRequest': {'containers': [{
                'resources': {'cpu': '2000m', 'mem': '2048Mi'},
                'envs': [{'key': 'API_KEY', 'value': 'SECRET-SENTINEL'}],
                'image': {'image': 'rfs-example', 'annotations': {
                    'cube.master.rootfs.artifact.sha256': 'a' * 64,
                    'cube.master.rootfs.artifact.token': 'SECRET-SENTINEL',
                    'cube.master.rootfs.artifact.url': 'http://internal/?token=SECRET-SENTINEL',
                }},
            }]},
        }

    def capture(self, detail=None):
        response = io.BytesIO(json.dumps(self.detail if detail is None else detail).encode())
        with mock.patch.object(cube, 'urlopen', return_value=response):
            return cube.capture_cube_environment(api_url='http://127.0.0.1:3000',
                template='canonical', sdk_path=self.sdk, phase_binary=self.binary)

    def test_actual_resources_replace_unrelated_defaults_and_secrets_are_excluded(self):
        result = self.capture()
        self.assertEqual(result['template_cpu_millicores'], 2000)
        self.assertEqual(result['template_memory_mb'], 2048)
        self.assertNotIn('SECRET-SENTINEL', json.dumps(result))
        self.assertEqual(result['cubelet_binary']['sha256'],
                         hashlib.sha256(self.binary.read_bytes()).hexdigest())
        self.assertEqual(result['sdk']['python_files'][0]['path'], 'cubesandbox/__init__.py')

    def test_not_ready_wrong_identity_and_missing_resources_fail_before_execution(self):
        for patch in ({'status': 'FAILED'}, {'templateID': 'other'}, {'createRequest': {}}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.capture({**self.detail, **patch})
        missing = copy.deepcopy(self.detail)
        del missing['createRequest']['containers'][0]['resources']['mem']
        with self.assertRaisesRegex(ValueError, 'memory'):
            self.capture(missing)

    def test_fractional_cpu_and_gib_memory_are_converted_without_rounding(self):
        detail = copy.deepcopy(self.detail)
        detail['createRequest']['containers'][0]['resources'] = {'cpu': '0.5', 'mem': '0.5Gi'}
        result = self.capture(detail)
        self.assertEqual(result['template_cpu_millicores'], 500)
        self.assertEqual(result['template_memory_mb'], 512)
        detail['createRequest']['containers'][0]['resources']['mem'] = '500M'
        with self.assertRaisesRegex(ValueError, 'whole number'):
            self.capture(detail)

    def test_changed_sdk_file_changes_recorded_input_identity(self):
        first = self.capture()['sdk']['manifest_sha256']
        (self.sdk / 'cubesandbox/__init__.py').write_text('# changed SDK\n')
        second = self.capture()['sdk']['manifest_sha256']
        self.assertNotEqual(first, second)


if __name__ == '__main__':
    unittest.main()
