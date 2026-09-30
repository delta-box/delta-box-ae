"""Exercise the real fanout entry with SDK execution and service changes stubbed."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


class FanoutLayoutTests(unittest.TestCase):
    def invoke(self, node, *, hosted=True, remote=False, execution='local', admission_error=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / 'result'
            config = {'measurement': {'numa_node': node, 'cpus': {0:'0-3', 1:'28-31', 2:'48-51', 3:'72-75'}.get(node, '0-3')},
                      'e2b': {'execution': execution, 'template': 'template',
                              'api_url': 'https://remote.example' if remote else 'http://127.0.0.1:3100',
                              'sandbox_url': 'https://remote.example' if remote else 'http://localhost:3102'}}
            placements, commands = [], []
            def write_json(path, value):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value))
            def execute(command, *args, **kwargs):
                commands.append(command)
                write_json(out/'fanout.json', [{'forks':n, 'success':True, 'success_count':n, 'ready_e2e_ms':1} for n in (1,4)])
                return {'status':'ok'}
            @contextmanager
            def service_placement(cfg, path, **kwargs):
                placements.append((cfg, kwargs))
                if admission_error:
                    raise ValueError(admission_error)
                yield {'node':cfg['measurement']['numa_node']}
            modules = {
                'repro.common': types.SimpleNamespace(host_state=lambda:{}, run_purpose=lambda x:x,
                    artifact_records=lambda *a:[], AE_ROOT=root, REPO_ROOT=root, load_config=lambda p:config,
                    configured_path=lambda *a:root, file_record=lambda p:{}, write_json=write_json,
                    number=lambda *a:None, repository_state=lambda:{}),
                'repro.process': types.SimpleNamespace(execute=execute),
                'repro.fanout_sdk': types.SimpleNamespace(fanout_python=lambda *a:'/python',
                    probe_e2b_sdk=lambda *a, **k:{'ok':True, 'api_key':'set'}),
                'release.lock': types.SimpleNamespace(from_environment=lambda:{'source_sha256':'a'*64}),
                'ae.scripts.e2b_service_context': types.SimpleNamespace(assert_backend_ready=lambda:None,
                    service_placement=service_placement),
            }
            environment = {'AE_HOSTED_CALLER_UID':'1012'} if hosted else {}
            argv = ['fanout.py', '--backend', 'e2b', '--config', 'config.json', '--out', str(out), '--forks', '1,4']
            with patch.dict(sys.modules, modules), patch.dict(os.environ, environment, clear=True), patch.object(sys, 'argv', argv):
                spec = importlib.util.spec_from_file_location('fanout_layout_fixture', ROOT/'ae/runners/fanout.py')
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if admission_error:
                    with self.assertRaisesRegex(ValueError, admission_error):
                        module.main()
                    self.assertEqual(commands, [])
                else:
                    self.assertEqual(module.main(), 0)
            # The output path is the only per-run command difference.
            for command in commands:
                command[command.index('--out') + 1] = '<output>'
                command[1] = '<same-original-driver>'
            return placements, commands

    def test_hosted_four_lanes_share_managed_ram_and_original_fork_command(self):
        expected = None
        for node in (0,1,2,3):
            placements, commands = self.invoke(node)
            self.assertEqual(len(placements), 1)
            cfg, options = placements[0]
            self.assertEqual(cfg['measurement']['numa_node'], node)
            self.assertTrue(options['working_storage'])
            self.assertEqual(options['source_sha256'], 'a'*64)
            expected = commands if expected is None else expected
            self.assertEqual(commands, expected)

    def test_self_managed_and_remote_keep_the_unmanaged_sdk_path(self):
        for options in ({'hosted':False}, {'remote':True}, {'execution':'ssh'}):
            for node in (0,1,2,3):
                with self.subTest(node=node, options=options):
                    placements, commands = self.invoke(node, **options)
                    self.assertEqual(placements, [])
                    self.assertEqual(len(commands), 1)

    def test_hosted_marker_does_not_bypass_real_service_admission(self):
        for node in (0,1,2,3,4):
            with self.subTest(node=node):
                self.invoke(node, admission_error='missing lease or invalid lane')


if __name__ == '__main__':
    unittest.main()
