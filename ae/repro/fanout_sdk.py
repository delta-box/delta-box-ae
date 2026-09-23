"""Select and inspect the SDK interpreter before sandbox fanout timing."""
from __future__ import annotations

import json
import os
import subprocess

from .common import AE_ROOT, REPO_ROOT, configured_path


def fanout_python(config, backend):
    if backend == 'e2b':
        selected = configured_path(config, 'e2b.fanout_python', required=False)
        if selected is not None:
            return selected
    return configured_path(config, 'moatless_venv') / 'bin/python'


_E2B_PROBE = r'''
import contextlib
import importlib.metadata
import inspect
import io
import json
import platform
import re
import sys

# Match the measured driver's module search path, including its caller-supplied
# PYTHONPATH, without starting a sandbox or invoking any SDK API method.
sys.path[0] = sys.argv[1]
sys.dont_write_bytecode = True
result = {'ok': False, 'python': {'executable': sys.executable,
                                'version': platform.python_version()}}
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        import e2b
        from e2b import Sandbox
        from e2b.sandbox_sync.commands.command import Commands
        required = ('create', 'create_snapshot', 'delete_snapshot', 'kill')
        missing = ['e2b.Sandbox.' + name for name in required
                   if not callable(getattr(Sandbox, name, None))]
        commands = inspect.getattr_static(Sandbox, 'commands', None)
        if not isinstance(commands, property) or not callable(commands.fget):
            missing.append('e2b.Sandbox.commands')
        if not callable(getattr(Commands, 'run', None)):
            missing.append('e2b.Commands.run')
        try:
            version = importlib.metadata.version('e2b')
        except importlib.metadata.PackageNotFoundError:
            version = getattr(e2b, '__version__', None)
        result['sdk'] = {'name': 'e2b', 'version': version,
                         'module_file': getattr(e2b, '__file__', None),
                         'sandbox_module_file': inspect.getfile(Sandbox),
                         'commands_module_file': inspect.getfile(Commands)}
        result['missing_interfaces'] = missing
        result['ok'] = not missing
        if missing:
            result['error'] = 'Missing callable SDK interfaces: ' + ', '.join(missing)
except Exception as error:
    # SDK import failures can include credentials in exception text or logging.
    # Report only the failing import and exception type; never captured output.
    module = getattr(error, 'name', None)
    module = ('; module=' + module) if isinstance(module, str) and re.fullmatch(r'[A-Za-z0-9_.]+', module) else ''
    result['error'] = 'Cannot import/inspect e2b.Sandbox (' + type(error).__name__ + module + ')'
print(json.dumps(result))
'''


def probe_e2b_sdk(python, *, env=None):
    """Import the real Sandbox class using the same Python/env as the driver."""
    environment = dict(os.environ if env is None else env)
    record = {'configured_python': str(python),
              'api_key': 'set' if environment.get('E2B_API_KEY') else 'missing'}
    command = [str(python), '-c', _E2B_PROBE,
               str(AE_ROOT / 'vendor/finalbench/official_sandbox_fork')]
    try:
        completed = subprocess.run(command, cwd=REPO_ROOT, env=environment,
                                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return dict(record, ok=False, error='E2B SDK Python probe failed (' + type(error).__name__ + ')')
    if completed.returncode:
        return dict(record, ok=False, error=f'E2B SDK Python probe exited with status {completed.returncode}')
    try:
        result = json.loads(completed.stdout)
        if not isinstance(result, dict) or type(result.get('ok')) is not bool:
            raise ValueError('invalid SDK probe response')
    except ValueError:
        return dict(record, ok=False, error='E2B SDK Python probe returned invalid JSON; captured output omitted')
    return dict(record, **result)
