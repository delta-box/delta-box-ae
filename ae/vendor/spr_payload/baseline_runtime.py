"""Explicit real test execution for replay. No recorded-result or no-op runtime.

An invocation owns all of its children and temporary files until it returns.
Only completed invocations may be checkpointed. Test execution, patch application,
and result parsing are workload costs; no timing is subtracted from a benchmark.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ENV = 'DELTABOX_BASELINE_TEST_RUNTIME'


def settings(value=None):
    if value is None:
        path = os.environ.get(ENV + "_FILE")
        value = json.loads(Path(path).read_text() if path else os.environ.get(ENV, '{"backend":"none"}'))
    if not isinstance(value, dict) or value.get('backend', 'none') not in ('none', 'local-pytest'):
        raise ValueError('baseline_test_runtime requires backend none or local-pytest')
    unknown = set(value) - {'backend', 'python', 'pytest_args', 'timeout_s', 'environment', 'auto_test_files', 'test_expression'}
    if unknown:
        raise ValueError(f'Unknown baseline_test_runtime fields: {sorted(unknown)}')
    result = {'backend': value.get('backend', 'none')}
    if result['backend'] == 'none':
        if len(value) > 1:
            raise ValueError('none runtime does not accept execution options')
        return result
    python = value.get('python', sys.executable)
    if not isinstance(python, str) or not Path(python).is_absolute():
        raise ValueError('local-pytest requires an absolute Python executable')
    timeout = value.get('timeout_s', 600)
    if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
        raise ValueError('test timeout_s must be in (0, 3600]')
    args = value.get('pytest_args', [])
    # Test selectors come only from FileContext. Extra options must not override
    # our result transport, inject alternate tests, or bypass pytest execution.
    allowed = {'--disable-warnings', '--strict-markers', '--strict-config', '-q', '-v', '-vv'}
    if not isinstance(args, list) or any(arg not in allowed for arg in args):
        raise ValueError(f'pytest_args supports only {sorted(allowed)}')
    env = value.get('environment', {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        raise ValueError('test environment must be a string mapping')
    forbidden = {'PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'}
    if forbidden.intersection(env):
        raise ValueError('Test environment must not override Python or pytest loading')
    automatic = value.get('auto_test_files', [])
    if not isinstance(automatic, list) or any(not isinstance(p, str) or not p or
            PurePosixPath(p).is_absolute() or '..' in PurePosixPath(p).parts or p.startswith('-') or
            str(PurePosixPath(p)) != p or '::' in p for p in automatic):
        raise ValueError('auto_test_files must contain explicit relative file paths')
    expression = value.get('test_expression')
    if expression is not None and (not isinstance(expression, str) or not expression.strip() or len(expression) > 4096):
        raise ValueError('test_expression must be an explicit nonempty pytest -k expression')
    return dict(result, python=python, timeout_s=timeout, pytest_args=args, environment=env,
                auto_test_files=list(dict.fromkeys(automatic)), test_expression=expression)


def describe(value):
    config = settings(value)
    return {**config, 'executes_tests': config['backend'] != 'none',
            'result_semantics': 'real pytest cases; no gold test patch; requested files only' if config['backend'] != 'none' else 'tests_not_executed',
            'timing': 'all test execution is included in workload time',
            'state_contract': 'synchronous owned subprocesses reaped before checkpoint; stateless across restore',
            'test_discovery': 'explicit auto_test_files; no external embedding or inferred test selection'}


def _run(command, *, cwd, timeout, env=None, input=None):
    """Kill/reap the complete owned process group, including on interruption."""
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        output, _ = proc.communicate(input=input, timeout=timeout)
        return proc.returncode, output.decode('utf-8', errors='replace')
    finally:
        # A successful pytest parent must not leave test-spawned children alive.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def parse_junit(path, returncode, requested):
    """Map actual per-case results; never infer that a file passed from rc=0."""
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    if not cases or returncode not in (0, 1, 2):
        raise RuntimeError(f'pytest did not produce a valid test run: rc={returncode}, cases={len(cases)}')
    results = []
    for case in cases:
        file = case.get('file')
        # xunit1 supplies file for normal tests. Collection errors may omit it;
        # their classname is pytest's path of the failed collection item.
        if not file:
            name = case.get('classname', '')
            if name in requested:
                file = name
            elif len(requested) == 1:
                file = requested[0]
            else:
                raise RuntimeError('JUnit case lacks an unambiguous requested file')
        if file not in requested:
            raise RuntimeError(f'JUnit case is outside requested files: {file}')
        status, message = 'PASSED', None
        for tag, mapped in (('error', 'ERROR'), ('failure', 'FAILED'), ('skipped', 'SKIPPED')):
            child = case.find(tag)
            if child is not None:
                status = mapped
                message = (child.text or child.get('message') or '')[-16000:]
                break
        results.append({'name': case.get('name'), 'file_path': file, 'status': status,
                        'message': message, 'line': int(case.get('line')) + 1 if case.get('line') else None})
    bad = any(r['status'] in ('FAILED', 'ERROR') for r in results)
    if (returncode == 0 and bad) or (returncode in (1, 2) and not bad):
        raise RuntimeError('pytest exit status and JUnit case results disagree')
    return results


class LocalPytestRuntime:
    """Private base checkout + FileContext patch; no changes to live repository."""
    def __init__(self, repository, commit, config):
        self.repository = Path(repository.repo_path).resolve()
        self.commit = commit
        self.config = settings(config)
        self.failure = None
        self.records = []
        if not isinstance(commit, str) or len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
            raise ValueError('Runtime requires the full trace base commit')
        if not Path(self.config['python']).is_file():
            raise FileNotFoundError(self.config['python'])

    def find_test_files(self, file_path, **kwargs):
        # Moatless's original implementation unconditionally invokes embeddings,
        # even before its filename heuristic. Offline replay must not trigger a
        # paid/network model simply because real tests are now bound.
        from moatless.schema import FileWithSpans
        files = self.config['auto_test_files']
        if not files:
            self.failure = ('Editing a source file requires explicitly configured auto_test_files; '
                            'implicit remote embedding test discovery is disabled')
            raise RuntimeError(self.failure)
        return [FileWithSpans(file_path=name, span_ids=[]) for name in files]

    def assert_healthy(self):
        if self.failure:
            raise RuntimeError(f'Test runtime infrastructure failure: {self.failure}')

    def run_tests(self, patch=None, test_files=None):
        from moatless.runtime.runtime import TestResult
        started = time.perf_counter()
        try:
            if not isinstance(test_files, list) or not test_files or any(not isinstance(p, str) for p in test_files):
                raise ValueError('Real test execution requires an explicit nonempty test_files list')
            files = list(dict.fromkeys(test_files))
            for name in files:
                path = PurePosixPath(name)
                if path.is_absolute() or '..' in path.parts or name.startswith('-') or str(path) != name or '::' in name:
                    raise ValueError(f'Unsafe or unsupported test file: {name!r}')
            if patch is not None and not isinstance(patch, str):
                raise ValueError('patch must be a string or None')
            config = self.config
            with tempfile.TemporaryDirectory(prefix='deltabox-pytest-') as temporary:
                root = Path(temporary)
                checkout = root / 'repo'
                for cmd in (['git', 'clone', '--quiet', '--shared', '--no-checkout', str(self.repository), str(checkout)],
                            ['git', '-C', str(checkout), '-c', 'core.hooksPath=/dev/null', 'checkout', '--quiet', '--detach', self.commit]):
                    rc, output = _run(cmd, cwd=root, timeout=60)
                    if rc:
                        raise RuntimeError(f'Private test checkout failed: {output[-2000:]}')
                if patch:
                    content = (patch.rstrip('\n') + '\n').encode()
                    for flag in ('--check', '--apply'):
                        rc, output = _run(['git', 'apply', flag, '--whitespace=nowarn', '-'],
                                          cwd=checkout, timeout=30, input=content)
                        if rc:
                            raise RuntimeError(f'FileContext patch did not apply: {output[-2000:]}')
                for name in files:
                    path = (checkout / name).resolve()
                    if not path.is_relative_to(checkout) or not path.is_file():
                        raise RuntimeError(f'Requested test file missing or escapes checkout: {name}')
                env = dict(os.environ)
                for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
                    env.pop(name, None)
                env.update(config['environment'])
                env['PYTHONNOUSERSITE'] = '1'
                report = root / 'results.xml'
                command = [config['python'], '-m', 'pytest', *config['pytest_args'],
                           '-o', 'addopts=', '-o', 'junit_family=xunit1', '--junitxml', str(report), '--', *files]
                if config['test_expression']:
                    command[3:3] = ['-k', config['test_expression']]
                rc, output = _run(command, cwd=checkout, timeout=config['timeout_s'], env=env)
                try:
                    raw = parse_junit(report, rc, files)
                except Exception as error:
                    raise RuntimeError(f'{error}; pytest output: {output[-4000:]}') from error
                # Diagnostic evidence stays in memory. Caller exports it after
                # its measurement; no print/hash/file logging on this path.
                self.records.append({'test_files': files, 'returncode': rc,
                                     'results': raw, 'output_tail': output[-16000:],
                                     'wall_s': time.perf_counter() - started})
                return [TestResult(**result) for result in raw]
        except BaseException as error:
            self.failure = f'{type(error).__name__}: {error}'
            raise


def install_result_semantics():
    """Correct upstream skipped-as-passed summaries only for this runtime."""
    from moatless.file_context import FileContext
    from moatless.runtime.runtime import TestStatus
    if getattr(FileContext, '_deltabox_local_pytest_semantics', False):
        return
    original_counts = FileContext.get_test_counts
    original_summary = FileContext.get_test_summary
    original_status = FileContext.get_test_status

    def statuses(context):
        return [r.status for file in context._test_files.values() for r in file.test_results]

    def counts(context):
        if not isinstance(context._runtime, LocalPytestRuntime):
            return original_counts(context)
        values = statuses(context)
        return tuple(values.count(status) for status in (TestStatus.PASSED, TestStatus.FAILED, TestStatus.ERROR))

    def summary(context):
        if not isinstance(context._runtime, LocalPytestRuntime):
            return original_summary(context)
        passed, failed, errors = counts(context)
        skipped = statuses(context).count(TestStatus.SKIPPED)
        text = f'{passed} passed. {failed} failed. {errors} errors.'
        return text + (f' {skipped} skipped.' if skipped else '')

    def status(context):
        if not isinstance(context._runtime, LocalPytestRuntime):
            return original_status(context)
        values = statuses(context)
        if not values:
            return None
        for code in (TestStatus.ERROR, TestStatus.FAILED, TestStatus.SKIPPED):
            if code in values:
                return code
        return TestStatus.PASSED

    FileContext.get_test_counts = counts
    FileContext.get_test_summary = summary
    FileContext.get_test_status = status
    FileContext._deltabox_local_pytest_semantics = True


def build_runtime(repository, tree_dict, *, config=None, code_index=None):
    chosen = settings(config)
    if chosen['backend'] == 'none':
        return None
    install_result_semantics()
    runtime = LocalPytestRuntime(repository, tree_dict.get('repository', {}).get('commit'), chosen)
    if code_index is not None:
        code_index.find_test_files = runtime.find_test_files
    return runtime


def check_runtime(runtime):
    if runtime is not None:
        runtime.assert_healthy()


def runtime_records(runtime):
    return list(runtime.records) if runtime is not None else []
