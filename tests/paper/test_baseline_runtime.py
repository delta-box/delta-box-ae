"""Test actual pytest execution/result transport, isolation, and fail-closed binding."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae/vendor/spr_payload'))
import baseline_runtime as runtime


class RuntimeContractTests(unittest.TestCase):
    def report(self, xml, code=0, files=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'results.xml'; path.write_text(xml)
            return runtime.parse_junit(path, code, files or ['test_x.py'])

    def test_case_results_are_not_exit_code_guesses(self):
        records = self.report('<testsuite><testcase file="test_x.py" name="ok"/>'
            '<testcase file="test_x.py" name="bad"><failure>assertion failed</failure></testcase>'
            '<testcase file="test_x.py" name="err"><error>setup failed</error></testcase>'
            '<testcase file="test_x.py" name="skip"><skipped>skip reason</skipped></testcase></testsuite>', 1)
        self.assertEqual([r['status'] for r in records], ['PASSED', 'FAILED', 'ERROR', 'SKIPPED'])
        self.assertEqual(records[1]['message'], 'assertion failed')

    def test_empty_and_status_disagreement_fail(self):
        for xml, rc in [('<testsuite/>', 0), ('<testsuite><testcase name="ok"/></testsuite>', 1),
                        ('<testsuite><testcase name="bad"><failure/></testcase></testsuite>', 0)]:
            with self.assertRaises(RuntimeError): self.report(xml, rc)

    def test_ambiguous_case_file_fails(self):
        with self.assertRaises(RuntimeError):
            self.report('<testsuite><testcase name="ok"/></testsuite>', files=['a.py', 'b.py'])

    def test_noop_runtime_is_explicit_none(self):
        with patch.dict(os.environ, {runtime.ENV: '{"backend":"none"}'}, clear=True):
            self.assertIsNone(runtime.build_runtime(None, {}))
        self.assertFalse(runtime.describe({'backend': 'none'})['executes_tests'])

    def test_runtime_conditions_cannot_share_a_plot_group(self):
        sys.path.insert(0, str(ROOT / 'ae'))
        from repro.analysis import fresh_labels
        none = fresh_labels({'experiment': 'table-02'})
        a = fresh_labels({'experiment': 'table-02', 'baseline_test_runtime':
            {'backend': 'local-pytest', 'python': '/a/python', 'environment_identity': {'pytest': '8'}}})
        b = fresh_labels({'experiment': 'table-02', 'baseline_test_runtime':
            {'backend': 'local-pytest', 'python': '/a/python', 'environment_identity': {'pytest': '7'}}})
        self.assertNotEqual(none['plot_group'], a['plot_group'])
        self.assertNotEqual(a['plot_group'], b['plot_group'])

    @unittest.skipUnless(sys.platform == 'linux', 'Linux process-state check')
    def test_timeout_kills_owned_child_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / 'child.pid'
            code = ('import subprocess,sys,time; '
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
                f'open({str(pidfile)!r},"w").write(str(p.pid)); time.sleep(60)')
            with self.assertRaises(subprocess.TimeoutExpired):
                runtime._run([sys.executable, '-c', code], cwd=tmp, timeout=1)
            pid = int(pidfile.read_text())
            status = Path(f'/proc/{pid}/status')
            # killpg delivers SIGKILL asynchronously; _run waits for the direct
            # child, while its descendant may still be completing its exit.
            deadline = time.monotonic() + 0.5
            while True:
                try:
                    state = next(line for line in status.read_text().splitlines()
                                 if line.startswith('State:')).split()[1]
                except (FileNotFoundError, ProcessLookupError):
                    break  # /proc may disappear during the read itself.
                if state in ('Z', 'X'):
                    break
                if time.monotonic() >= deadline:
                    self.fail(f'owned child is still active after timeout: pid={pid}, state={state}')
                time.sleep(0.005)

    def test_skipped_cases_are_not_reported_as_passed_in_bound_history(self):
        from enum import Enum
        class Status(str, Enum):
            PASSED = 'PASSED'; FAILED = 'FAILED'; ERROR = 'ERROR'; SKIPPED = 'SKIPPED'
        class Context:
            def get_test_counts(self): return (77, 0, 0)
            def get_test_summary(self): return 'legacy'
            def get_test_status(self): return 'legacy'
        context_module = types.ModuleType('moatless.file_context'); context_module.FileContext = Context
        result_module = types.ModuleType('moatless.runtime.runtime'); result_module.TestStatus = Status
        with patch.dict(sys.modules, {'moatless.file_context': context_module, 'moatless.runtime.runtime': result_module}):
            runtime.install_result_semantics()
            context = Context(); context._runtime = runtime.LocalPytestRuntime.__new__(runtime.LocalPytestRuntime)
            context._test_files = {'test_x.py': types.SimpleNamespace(test_results=[
                types.SimpleNamespace(status=Status.PASSED), types.SimpleNamespace(status=Status.SKIPPED)])}
            self.assertEqual(context.get_test_counts(), (1, 0, 0))
            self.assertEqual(context.get_test_summary(), '1 passed. 0 failed. 0 errors. 1 skipped.')
            self.assertEqual(context.get_test_status(), Status.SKIPPED)
            context._runtime = None
            self.assertEqual(context.get_test_summary(), 'legacy')
            self.assertEqual(context.get_test_counts(), (77, 0, 0))

    def test_compacted_checkpoint_results_are_counted_once(self):
        spec = importlib.util.spec_from_file_location('runtime_binding_baseline_runner', ROOT / 'ae/runners/baseline.py')
        baseline = importlib.util.module_from_spec(spec); spec.loader.exec_module(baseline)
        records = [{'results': [{'status': 'PASSED'}, {'status': 'SKIPPED'}]}]
        checkpoint = {'test_runtime_records': records}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'pilot_result.json'
            path.write_text(json.dumps({'ckpts': [checkpoint], 'checkpoints': [checkpoint]}))
            result = baseline.test_execution_summary(path, 'criu')
        self.assertEqual(result['invocations'], 1)
        self.assertEqual(result['actual_case_status_counts']['PASSED'], 1)
        self.assertEqual(result['actual_case_status_counts']['SKIPPED'], 1)
        self.assertFalse(result['all_selected_cases_passed'])

    def test_bad_config_cannot_fall_back(self):
        for config in ({'backend': 'NoEnvironment'}, {'backend': 'none', 'python': '/python'},
                       {'backend': 'local-pytest', 'pytest_args': ['--collect-only']},
                       {'backend': 'local-pytest', 'environment': {'PYTEST_ADDOPTS': '--collect-only'}}):
            with self.assertRaises(ValueError): runtime.settings(config)


@unittest.skipUnless(importlib.util.find_spec('pytest'), 'requires real pytest')
class RealPytestRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'; self.repo.mkdir()
        self.git('init', '-q'); self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        (self.repo / 'test_sample.py').write_text('def test_good():\n    assert True\n\ndef test_bad():\n    assert False\n')
        self.git('add', '.'); self.git('commit', '-qm', 'base')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.adapter = runtime.LocalPytestRuntime(types.SimpleNamespace(repo_path=str(self.repo)), self.commit,
            {'backend': 'local-pytest', 'python': sys.executable, 'timeout_s': 20})
        # This test exercises real git/pytest/JUnit; only the heavy Moatless DTO
        # import is replaced. Remote integration separately uses actual DTOs.
        mod = types.ModuleType('moatless.runtime.runtime')
        mod.TestResult = lambda **kwargs: types.SimpleNamespace(**kwargs)
        self.modules = patch.dict(sys.modules, {'moatless.runtime.runtime': mod}); self.modules.start()
        self.addCleanup(self.modules.stop)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True, stderr=subprocess.STDOUT)

    def test_real_pass_fail_patch_and_live_checkout_unchanged(self):
        original = (self.repo / 'test_sample.py').read_text()
        results = self.adapter.run_tests(test_files=['test_sample.py'])
        self.assertEqual([r.status for r in results], ['PASSED', 'FAILED'])
        (self.repo / 'test_sample.py').write_text(original.replace('assert False', 'assert True'))
        delta = self.git('diff'); (self.repo / 'test_sample.py').write_text(original)
        patched = self.adapter.run_tests(patch=delta, test_files=['test_sample.py'])
        self.assertEqual([r.status for r in patched], ['PASSED', 'PASSED'])
        self.assertEqual((self.repo / 'test_sample.py').read_text(), original)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual([r['returncode'] for r in self.adapter.records], [1, 0])

    def test_collection_failure_is_error_not_zero_success(self):
        (self.repo / 'test_sample.py').write_text('import definitely_missing_deltabox_fixture\n')
        delta = self.git('diff')
        results = self.adapter.run_tests(patch=delta, test_files=['test_sample.py'])
        self.assertTrue(any(r.status == 'ERROR' for r in results))
        self.assertFalse(any(r.status == 'PASSED' for r in results))

    def test_patch_failure_latches_infrastructure_error(self):
        with self.assertRaises(RuntimeError):
            self.adapter.run_tests(patch='not a patch', test_files=['test_sample.py'])
        with self.assertRaises(RuntimeError): self.adapter.assert_healthy()

    def test_source_edit_discovery_is_explicit_and_offline(self):
        mod = types.ModuleType('moatless.schema')
        mod.FileWithSpans = lambda **kwargs: types.SimpleNamespace(**kwargs)
        with patch.dict(sys.modules, {'moatless.schema': mod}):
            with self.assertRaisesRegex(RuntimeError, 'auto_test_files'):
                self.adapter.find_test_files('source.py', query='source.py')
            configured = runtime.LocalPytestRuntime(types.SimpleNamespace(repo_path=str(self.repo)), self.commit,
                {'backend': 'local-pytest', 'python': sys.executable, 'auto_test_files': ['test_sample.py']})
            self.assertEqual([r.file_path for r in configured.find_test_files('source.py')], ['test_sample.py'])

    def test_invalid_requested_file_cannot_escape(self):
        with self.assertRaises(ValueError): self.adapter.run_tests(test_files=['../test.py'])
        with self.assertRaises(RuntimeError): self.adapter.assert_healthy()


if __name__ == '__main__': unittest.main()
