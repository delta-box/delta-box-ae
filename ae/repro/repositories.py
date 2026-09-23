"""Read locally cached, immutable Git objects without any lazy-fetch transport.

Git 2.34 predates GIT_NO_LAZY_FETCH. Its transport.c gives GIT_ALLOW_PROTOCOL
priority over per-protocol/repository config; an empty whitelist denies every
transport, including promisor fetch subprocesses that inherit this environment.
No repository configuration is edited and no remote helper may be started.
References: https://github.com/git/git/blob/v2.34.1/transport.c
            https://github.com/git/git/blob/v2.34.1/promisor-remote.c
"""
import os
from pathlib import Path
import re
import signal
import subprocess

from .common import configured_path


class LocalObjectReadError(RuntimeError):
    """An offline Git probe failed abnormally, rather than proving absence."""


def offline_environment():
    env = dict(os.environ)
    # The caller's checkout/index/object-store variables must not redirect -C.
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(name, None)
    env.update(GIT_ALLOW_PROTOCOL='', GIT_TERMINAL_PROMPT='0',
               GIT_NO_REPLACE_OBJECTS='1', GIT_OPTIONAL_LOCKS='0', LC_ALL='C')
    return env


def offline_git(repository, *args, timeout=30):
    """Run an owned, transport-disabled Git tree; kill only its session on exit."""
    command = ['git', '-C', str(repository), *args]
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=offline_environment(), start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as error:
        raise LocalObjectReadError(f'Local Git object probe timed out after {timeout}s: {repository}') from error
    finally:
        # communicate() may time out while a descendant holds the pipe, or the
        # leading Git may exit first. The new session belongs exclusively to us.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate()


def validate_object(commit, name=None):
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
        raise ValueError('A full recorded Git commit is required')
    if name is not None and (not isinstance(name, str) or not name or Path(name).is_absolute()
                             or '..' in Path(name).parts or '\0' in name):
        raise ValueError('Recorded files must be relative repository paths')


def local_type(repository, obj):
    result = offline_git(repository, 'cat-file', '-t', obj)
    if result.returncode == 0:
        return result.stdout.decode('ascii').strip()
    if result.returncode < 0:
        raise LocalObjectReadError(f'Local Git object probe terminated by signal {-result.returncode}: {repository}')
    # Only Git's missing-object/path diagnostics prove unavailability. A corrupt
    # object store, unsafe ownership, bad config or other execution fault must
    # propagate, so the reviewer cannot mistake an infrastructure failure for
    # an optional missing dataset. LC_ALL=C makes this check deterministic.
    detail = result.stderr.decode('utf-8', errors='replace').strip()
    missing = ('could not get object info', 'Not a valid object name',
               'does not exist in', 'exists on disk, but not in')
    if any(message in detail for message in missing):
        return None
    raise LocalObjectReadError(f'Local Git object probe failed (exit {result.returncode}): {repository}; {detail[-1000:]}')


def read_blob(repository, commit, name):
    """Read the exact recorded blob locally; metadata/path existence is insufficient."""
    validate_object(commit, name)
    result = offline_git(repository, 'cat-file', 'blob', commit + ':' + name)
    if result.returncode != 0:
        detail = result.stderr.decode('utf-8', errors='replace').strip()[-1000:]
        raise LocalObjectReadError(f'Recorded blob is not locally readable: {repository} {commit}:{name}; {detail}')
    return result.stdout


def select_repository(config, instance, commit, required_files=()):
    if not isinstance(instance, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+', instance):
        raise ValueError('Invalid repository instance identity')
    validate_object(commit)
    names = tuple(required_files)
    for name in names:
        validate_object(commit, name)
    mode = config.get('repository_fallback', 'none')
    if mode not in ('none', 'same-project-commit'):
        raise ValueError('repository_fallback must be none or same-project-commit')
    repositories = configured_path(config, 'payload') / 'repos'
    primary = repositories / ('swe-bench_' + instance)
    candidates = [primary]
    if mode == 'same-project-commit':
        project = instance.rsplit('-', 1)[0]
        candidates.extend(path for path in sorted(repositories.glob('swe-bench_' + project + '-*'))
                          if path != primary and path.is_dir())
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if local_type(candidate, commit) == 'commit' and all(
                local_type(candidate, commit + ':' + name) == 'blob' for name in names):
            return candidate
    raise FileNotFoundError(f'No {mode} repository has the recorded commit and required blobs cached locally for {instance}@{commit}; expected {primary}; network fetch is disabled')
