"""Exercise real interpreter imports without contacting a sandbox API."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'ae')]
from repro.fanout_sdk import fanout_python, probe_e2b_sdk


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = load('sdk_doctor_fixture', ROOT / 'ae/reproduce.py')
FANOUT = load('sdk_fanout_fixture', ROOT / 'ae/runners/fanout.py')
SDK = '''
__version__ = 'fixture-2.25.1'
class Sandbox:
    @property
    def commands(self):
        raise AssertionError('The import probe must not access a sandbox instance')
    @classmethod
    def create(cls, **kwargs):
        raise AssertionError('The import probe must not call the API')
    def create_snapshot(self, **kwargs):
        raise AssertionError('The import probe must not call the API')
    @classmethod
    def delete_snapshot(cls, *args, **kwargs):
        raise AssertionError('The import probe must not call the API')
    def kill(self, **kwargs):
        raise AssertionError('The import probe must not call the API')
'''


class FanoutSdkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, E2B_API_KEY='sdk-fixture-key')
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(temporary.name)
        self.good = self.make_python('independent', SDK)
        self.legacy = self.make_python('legacy', None)
        self.config = {'moatless_venv': str(self.legacy.parent.parent),
                       'e2b': {'fanout_python': str(self.good),
                               'api_url': 'http://localhost:1',
                               'sandbox_url': 'http://localhost:2', 'template': 'fixture'}}

    def make_python(self, name, sdk):
        root = self.root / name
        package = root / 'modules/e2b'
        package.mkdir(parents=True)
        if sdk is not None:
            (package / '__init__.py').write_text(sdk)
            commands = package / 'sandbox_sync/commands'
            commands.mkdir(parents=True)
            (commands.parent / '__init__.py').touch()
            (commands / '__init__.py').touch()
            (commands / 'command.py').write_text("class Commands:\n    def run(self, *args, **kwargs):\n"
                                               "        raise AssertionError('The probe must not execute commands')\n")
        python = root / 'bin/python'
        python.parent.mkdir()
        # -S ensures these fixtures cannot accidentally import a real SDK from
        # the developer's machine. Each executable has its own module directory.
        python.write_text('#!/bin/sh\nexport PYTHONPATH=' + shlex.quote(str(package.parent)) +
                          '\nexec ' + shlex.quote(sys.executable) + ' -S "$@"\n')
        python.chmod(0o755)
        return python

    def test_selection_uses_independent_e2b_python_and_keeps_legacy_fallback(self):
        self.assertEqual(fanout_python(self.config, 'e2b'), self.good)
        self.assertEqual(fanout_python(self.config, 'cube'), self.legacy)
        del self.config['e2b']['fanout_python']
        self.assertEqual(fanout_python(self.config, 'e2b'), self.legacy)

    def test_namespace_does_not_count_as_an_installed_sandbox_sdk(self):
        result = probe_e2b_sdk(self.legacy)
        self.assertFalse(result['ok'])
        self.assertIn('e2b.Sandbox', result['error'])
        self.assertIn('ImportError', result['error'])

    def test_real_import_records_versions_sources_and_never_calls_api(self):
        result = probe_e2b_sdk(self.good)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['configured_python'], str(self.good))
        self.assertEqual(result['sdk']['version'], 'fixture-2.25.1')
        self.assertEqual(result['python']['version'], '.'.join(map(str, sys.version_info[:3])))
        self.assertEqual(Path(result['sdk']['sandbox_module_file']), self.root / 'independent/modules/e2b/__init__.py')
        self.assertEqual(result['missing_interfaces'], [])

    def test_missing_snapshot_or_cleanup_interface_is_reported(self):
        for name in ('create', 'create_snapshot', 'delete_snapshot', 'kill', 'commands'):
            with self.subTest(interface=name):
                python = self.make_python('missing-' + name, SDK + '\nSandbox.' + name + ' = None\n')
                result = probe_e2b_sdk(python)
                self.assertFalse(result['ok'])
                self.assertIn('e2b.Sandbox.' + name, result['missing_interfaces'])

    def test_missing_command_execution_interface_is_reported(self):
        commands = self.root / 'independent/modules/e2b/sandbox_sync/commands/command.py'
        commands.write_text('class Commands:\n    run = None\n')
        result = probe_e2b_sdk(self.good)
        self.assertFalse(result['ok'])
        self.assertIn('e2b.Commands.run', result['missing_interfaces'])

    def test_import_errors_and_logging_do_not_expose_credentials(self):
        python = self.make_python('broken', "import os,sys\nprint(os.environ['E2B_API_KEY'])\n"
                                  "print(os.environ['E2B_API_KEY'],file=sys.stderr)\n"
                                  "raise RuntimeError(os.environ['E2B_API_KEY'])\n")
        result = probe_e2b_sdk(python, env=dict(os.environ, E2B_API_KEY='SECRET-SENTINEL'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['api_key'], 'set')
        self.assertNotIn('SECRET-SENTINEL', json.dumps(result))
        self.assertIn('RuntimeError', result['error'])

    def test_doctor_imports_the_selected_interpreter_not_the_moatless_namespace(self):
        result = CLI.doctor(self.config, ['figure-08-e2b'])
        self.assertTrue(result['ok'], result)
        checks = {row['name']: row for row in result['checks']}
        self.assertEqual(checks['e2b SDK python']['detail'], str(self.good))
        self.assertTrue(checks['e2b.Sandbox SDK']['ok'])
        del self.config['e2b']['fanout_python']
        result = CLI.doctor(self.config, ['figure-08-e2b'])
        self.assertFalse(result['ok'])
        sdk = next(row for row in result['checks'] if row['name'] == 'e2b.Sandbox SDK')
        self.assertFalse(sdk['ok'])
        self.assertIn('e2b.Sandbox', sdk['detail'])
        self.config['moatless_venv'] = str(self.good.parent.parent)
        result = CLI.doctor(self.config, ['figure-08-e2b'])
        self.assertTrue(result['ok'], result)

    def run_fanout(self, python, output, execute):
        self.config['e2b']['fanout_python'] = str(python)
        config = self.root / 'config.json'
        config.write_text(json.dumps(self.config))
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'argv', ['fanout.py', '--backend', 'e2b', '--config', str(config),
                                                          '--out', str(output), '--forks', '1']))
            stack.enter_context(patch.object(FANOUT, 'from_environment', return_value={}))
            stack.enter_context(patch.object(FANOUT, 'repository_state', return_value={}))
            stack.enter_context(patch.object(FANOUT, 'host_state', return_value={}))
            stack.enter_context(patch.object(FANOUT, 'execute', execute))
            return FANOUT.main()

    def test_driver_and_record_use_independent_interpreter_before_measurement(self):
        output = self.root / 'result'
        def measured(command, logdir, **kwargs):
            self.assertEqual(command[0], str(self.good))
            record = json.loads((output / 'run.json').read_text())
            self.assertTrue(record['e2b_environment']['ok'])
            self.assertEqual(record['e2b_environment']['sdk']['version'], 'fixture-2.25.1')
            (output / 'fanout.json').write_text(json.dumps([dict(forks=1, success=True, success_count=1, ready_e2e_ms=3.0)]))
            return {'status': 'ok'}
        self.assertEqual(self.run_fanout(self.good, output, measured), 0)
        self.assertEqual(json.loads((output / 'fanout.json').read_text())[0]['ready_e2e_ms'], 3.0)

    def test_failed_import_stops_before_driver_and_keeps_failure_record(self):
        from unittest.mock import Mock
        execute = Mock()
        output = self.root / 'failed'
        with self.assertRaisesRegex(ValueError, 'e2b.Sandbox'):
            self.run_fanout(self.legacy, output, execute)
        execute.assert_not_called()
        record = json.loads((output / 'run.json').read_text())
        self.assertEqual(record['status'], 'failed')
        self.assertFalse(record['e2b_environment']['ok'])

    def test_missing_api_key_fails_doctor_separately_from_working_sdk(self):
        with patch.dict(os.environ):
            os.environ.pop('E2B_API_KEY', None)
            result = CLI.doctor(self.config, ['figure-08-e2b'])
        self.assertFalse(result['ok'])
        checks = {row['name']: row for row in result['checks']}
        self.assertTrue(checks['e2b.Sandbox SDK']['ok'])
        self.assertEqual(checks['E2B_API_KEY'], {'name': 'E2B_API_KEY', 'ok': False, 'detail': 'missing'})

    def test_missing_api_key_stops_before_driver_but_preserves_sdk_diagnostics(self):
        from unittest.mock import Mock
        execute = Mock()
        output = self.root / 'missing-key'
        with patch.dict(os.environ):
            os.environ.pop('E2B_API_KEY', None)
            with self.assertRaisesRegex(ValueError, 'E2B_API_KEY is missing'):
                self.run_fanout(self.good, output, execute)
        execute.assert_not_called()
        record = json.loads((output / 'run.json').read_text())
        self.assertEqual(record['status'], 'failed')
        self.assertTrue(record['e2b_environment']['ok'])
        self.assertEqual(record['e2b_environment']['api_key'], 'missing')
        self.assertNotIn('sdk-fixture-key', json.dumps(record))


if __name__ == '__main__':
    unittest.main()
