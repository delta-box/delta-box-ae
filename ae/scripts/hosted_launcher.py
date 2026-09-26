#!/usr/bin/python3 -I
"""Start a fixed, trusted AE runtime through a restricted sudo entry point.

Install this file as /usr/local/sbin/deltabox-ae-run, owned by root and not
writable by reviewers. Its only policy is /etc/deltabox-ae/launcher.json.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import re
import stat
import struct
import sys
import subprocess

POLICY_PATH = Path('/etc/deltabox-ae/launcher.json')
POLICY_FIELDS = {'runtime_root', 'python', 'config', 'environment_file',
                 'output_root', 'allowed_user', 'lock_file'}
OPTIONAL_POLICY_FIELDS = {'trusted_maintainer', 'trusted_developer', 'temporary_root', 'results_backup_root', 'gpu_ssh_user'}
EXPERIMENTS = ('table-02-deltabox', 'table-03-slow', 'table-02-replay',
               'table-02-criu', 'table-02-fc-diff', 'table-02-cube', 'table-02-e2b',
               'figure-02-filesystem', 'figure-02-memory',
               'figure-06-memory', 'figure-06-adaptive', 'figure-08-deltabox',
               'figure-08-cube', 'figure-08-e2b', 'figure-09', 'correctness', 'figure-08-gpu')
GROUPS = ('deltabox', 'baselines', 'table-02', 'table-03', 'figure-02',
          'figure-06', 'figure-08', 'figure-08-cpu', 'gpu', 'cpu', 'figure-09', 'correctness')
API_ENVIRONMENT = {
    'E2B_API_KEY', 'E2B_API_URL', 'E2B_SANDBOX_URL', 'E2B_TEMPLATE', 'E2B_TEMPLATE_ID',
    'CUBE_API_KEY', 'CUBE_API_URL', 'CUBE_TEMPLATE', 'CUBE_TEMPLATE_ID',
    'CUBE_FINALBENCH_TEMPLATE', 'CUBE_PROXY_NODE_IP', 'CUBE_PROXY_PORT_HTTP',
    'CUBESANDBOX_API_KEY', 'CUBESANDBOX_API_URL',
}


class RuntimeTrust:
    """Root policy explicitly identifies accounts allowed to edit executed code."""
    def __init__(self, maintainer, developer=None):
        self.uids = {0, maintainer.pw_uid}
        self.developer_uid = None if developer is None else developer.pw_uid
        if developer is not None:
            self.uids.add(developer.pw_uid)
        self.groups = {}

    def group_writers_trusted(self, gid):
        if gid not in self.groups:
            group = grp.getgrgid(gid)
            # gr_mem omits users whose primary group is this group.
            members = {user.pw_uid for user in pwd.getpwall() if user.pw_gid == gid}
            members.update(pwd.getpwnam(name).pw_uid for name in group.gr_mem)
            self.groups[gid] = members <= self.uids
        return self.groups[gid]


def runtime_trust(policy):
    if 'trusted_maintainer' not in policy:
        if 'trusted_developer' in policy:
            raise ValueError('Developer execution requires a configured trusted maintainer')
        return None  # Existing policies keep their root-only ownership rules.
    maintainer = pwd.getpwnam(policy['trusted_maintainer'])
    allowed = pwd.getpwnam(policy['allowed_user'])
    developer = pwd.getpwnam(policy['trusted_developer']) if 'trusted_developer' in policy else None
    if developer is not None and developer.pw_uid != allowed.pw_uid:
        raise ValueError('The trusted developer must be the explicitly allowed caller')
    if maintainer.pw_uid == allowed.pw_uid and developer is None:
        raise ValueError('The reviewer cannot be the trusted maintainer')
    return RuntimeTrust(maintainer, developer)


def check_write_acl(path, info, trust, *, default=False):
    """Check Linux access/default ACLs, including writers of newly created files."""
    if not hasattr(os, 'getxattr'):
        raise ValueError(f'Cannot verify filesystem ACLs: {path}')
    try:
        acl = os.getxattr(path, 'system.posix_acl_default' if default else 'system.posix_acl_access',
                         follow_symlinks=False)
    except OSError as error:
        absent = {errno.ENODATA, errno.ENOTSUP, getattr(errno, 'ENOATTR', errno.ENODATA)}
        if error.errno in absent:
            return
        raise
    if len(acl) < 4 or (len(acl) - 4) % 8 or struct.unpack_from('<I', acl)[0] != 2:
        raise ValueError(f'Unrecognized filesystem ACL: {path}')
    entries = list(struct.iter_unpack('<HHI', acl[4:]))
    mask = next((permissions for tag, permissions, _ in entries if tag == 0x10), 0o7)
    owners = {0} if trust is None else trust.uids
    for tag, permissions, identity in entries:
        if tag not in (0x01, 0x02, 0x04, 0x08, 0x10, 0x20) or permissions & ~0o7:
            raise ValueError(f'Unrecognized filesystem ACL: {path}')
        if tag == 0x20 and permissions & 0o2 or permissions & mask & 0o2 and (
                tag == 0x02 and identity not in owners or
                tag in (0x04, 0x08) and (trust is None or not trust.group_writers_trusted(
                    info.st_gid if tag == 0x04 else identity))):
            raise ValueError(f'Path ACL is writable by an untrusted user or group: {path}')


def require_trusted_owned(path, info, *, trust=None, sticky_directory=False):
    if info.st_uid not in ({0} if trust is None else trust.uids):
        owner = 'root' if trust is None else 'root or the configured trusted maintainer'
        raise ValueError(f'Path must be owned by {owner}: {path}')
    if stat.S_ISLNK(info.st_mode):
        return  # Link permissions do not grant write access to the link itself.
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError(f'Expected a regular file or directory: {path}')
    protected_sticky = sticky_directory and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
    if not protected_sticky and (info.st_mode & 0o002 or
            info.st_mode & 0o020 and (trust is None or not trust.group_writers_trusted(info.st_gid))):
        raise ValueError(f'Path must not be writable by group or other users: {path}')
    if info.st_mode & 0o020 and not protected_sticky and trust is not None:
        check_write_acl(path, info, trust)
    if stat.S_ISDIR(info.st_mode) and not protected_sticky:
        check_write_acl(path, info, trust, default=True)


def require_root_owned(path, info, *, sticky_directory=False):
    require_trusted_owned(path, info, sticky_directory=sticky_directory)


def trusted_path(value, *, directory=False, symlinks=False, sticky_parents=False,
                 trust=None, root_leaf=False):
    """Check each existing component, including the target of a permitted link."""
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError(f'Expected an absolute path without parent traversal: {path}')
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        leaf_trust = trust
        if root_leaf and part == path:
            if stat.S_ISDIR(info.st_mode) and getattr(trust, 'developer_uid', None) is not None:
                # Developer-created children may inherit its ACL; the shared
                # result/temporary directory itself remains owned by root.
                if info.st_uid != 0:
                    raise ValueError(f'Protected directory must be owned by root: {path}')
            else:
                # Policy, environment, audit and producer control files keep
                # their root-only ownership/write checks in both modes.
                leaf_trust = None
        require_trusted_owned(part, info, trust=leaf_trust, sticky_directory=sticky_parents)
        if stat.S_ISLNK(info.st_mode):
            if not symlinks:
                raise ValueError(f'Symbolic links are not allowed in this path: {part}')
            trusted_path(part.resolve(strict=True), directory=part.is_dir(),
                         sticky_parents=sticky_parents, trust=trust)
    if directory != path.is_dir() or not directory and not path.is_file():
        raise ValueError(f'Unexpected path type: {path}')
    return path


def trusted_tree(root, *, code=False, external_code=False, seen=None, trust=None,
                 data_roots=(), environment_roots=(), resume_controls=False):
    """Inspect ownership without traversing links into shared input datasets."""
    root = trusted_path(root, directory=True, trust=trust)
    seen = set() if seen is None else seen
    identity = (root, code, external_code)
    if identity in seen:
        return
    seen.add(identity)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                if code or resume_controls:
                    raise
                continue
            require_trusted_owned(path, info, trust=trust)
            if code and path in (*data_roots, *environment_roots):
                if name in dirs:
                    dirs.remove(name)
                environment = path in environment_roots
                trusted_tree(path, code=environment, external_code=environment,
                             trust=trust, seen=seen, data_roots=data_roots)
                continue
            if resume_controls and (path.name in ('review.json', 'SUMMARY.md', 'suite.json', 'run.json', 'plan.json')
                    or path.relative_to(root).parts[0] in ('configs', 'plans', 'coverage', 'attempt-history')):
                trusted_path(path, directory=stat.S_ISDIR(info.st_mode), trust=trust, root_leaf=True)
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve(strict=True)
                if code and not target.is_relative_to(root) and not external_code:
                    raise ValueError(f'Runtime source link escapes the fixed checkout: {path}')
                if code and any(target.is_relative_to(data) for data in data_roots):
                    raise ValueError(f'Runtime source link enters generated data: {path}')
                trusted_path(target, directory=target.is_dir(), trust=trust)
                if code and target.is_dir() and not target.is_relative_to(root):
                    trusted_tree(target, code=True, external_code=True, seen=seen,
                                 trust=trust, data_roots=data_roots)


def read_root_json(path):
    path = trusted_path(path)
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f'Expected a JSON object: {path}')
    return value


def load_policy():
    policy = read_root_json(POLICY_PATH)
    if (not POLICY_FIELDS <= set(policy) or set(policy) - POLICY_FIELDS - OPTIONAL_POLICY_FIELDS
            or any(not isinstance(value, str) or not value for value in policy.values())):
        raise ValueError('Launcher policy requires: ' + ', '.join(sorted(POLICY_FIELDS)) +
                         '; optional: ' + ', '.join(sorted(OPTIONAL_POLICY_FIELDS)))
    for key in (POLICY_FIELDS - {'allowed_user'}) | ({'temporary_root', 'results_backup_root'} & set(policy)):
        path = Path(policy[key])
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError(f'Policy {key} must be an absolute path without parent traversal')
        policy[key] = path
    if 'gpu_ssh_user' in policy:
        account = pwd.getpwnam(policy['gpu_ssh_user'])
        if account.pw_uid == 0:
            raise ValueError('GPU SSH transport must use an unprivileged account')
    return policy


def caller_identity(policy):
    if os.geteuid() != 0:
        raise ValueError('Use the installed launcher through the configured sudo rule')
    allowed = pwd.getpwnam(policy['allowed_user'])
    value = os.environ.get('SUDO_UID')
    if value is not None and (not value.isascii() or not value.isdecimal()):
        raise ValueError('Invalid SUDO_UID')
    uid = int(value) if value is not None else os.getuid()
    if uid not in (0, allowed.pw_uid) or os.getuid() not in (0, uid):
        raise ValueError('Caller is not authorized by the launcher policy')
    return pwd.getpwuid(uid)


def positive_integer(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return number


class Once(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f'{option_string} may be supplied only once')
        setattr(namespace, self.dest, value)


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--checkout', required=True, type=Path, action=Once,
                        help='Checkout used by run_all.sh; must match the fixed runtime')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--all', action='store_true', help='Require all CPU and GPU experiments (default)')
    mode.add_argument('--test', dest='quick_check', action='store_true', help='Run the minimum DeltaBox check')
    mode.add_argument('--smoke', dest='quick_check', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--experiment', action='append', choices=EXPERIMENTS)
    parser.add_argument('--group', action='append', choices=GROUPS)
    parser.add_argument('--baseline-inputs', choices=('44', 'all'), action=Once,
                        help='Replay/CRIU/FC-diff: fixed 44 complete trajectories by default, or all inputs')
    parser.add_argument('--limit', type=positive_integer, action=Once)
    parser.add_argument('--max-events', type=positive_integer, action=Once)
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--output', type=Path, action=Once, help='New result path, relative to the fixed output root or absolute within it')
    output.add_argument('--resume', type=Path, action=Once, help='Existing result path, relative to the fixed output root or absolute within it')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--numa-node', type=int, action=Once, help='Quick-check NUMA node')
    parser.add_argument('--cpus', action=Once, help='Quick-check CPU list inside that node')
    args = parser.parse_args(argv)
    if args.quick_check and (args.experiment or args.group or args.limit is not None or args.max_events is not None):
        parser.error('--test already selects one DeltaBox instance and three events')
    if args.all and (args.experiment or args.group):
        parser.error('--all cannot be combined with a selected experiment/group')
    if args.list and (args.output or args.resume):
        parser.error('--list does not create or resume results')
    if args.numa_node is not None or args.cpus is not None:
        if not args.quick_check or args.numa_node is None or args.cpus is None:
            parser.error('--numa-node and --cpus must be supplied together with --test')
        if args.numa_node < 0 or not re.fullmatch(r'[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*', args.cpus):
            parser.error('Invalid quick-check NUMA/CPU placement')
    return args


def fixed_environment(policy, caller):
    supplied = read_root_json(policy['environment_file'])
    for key, value in supplied.items():
        if key not in API_ENVIRONMENT or not isinstance(value, str) or any(char in value for char in '\x00\r\n'):
            raise ValueError(f'Unsupported API environment entry: {key}')
    # No caller environment is copied: in particular no Python/Git/SSH loader
    # settings, SUDO_* ownership hints, AE_CONFIG, PATH, or release-lock override.
    environment = {
        **supplied,
        'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        'HOME': '/root', 'USER': 'root', 'LOGNAME': 'root',
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
        'AE_HOSTED_CALLER_UID': str(caller.pw_uid),
        'AE_HOSTED_CALLER_USER': caller.pw_name,
    }
    if 'temporary_root' in policy:
        environment['TMPDIR'] = str(policy['temporary_root'])
    if 'results_backup_root' in policy:
        environment['AE_RESULTS_BACKUP_ROOT'] = str(policy['results_backup_root'])
    if 'gpu_ssh_user' in policy:
        environment['AE_HOSTED_GPU_SSH_USER'] = policy['gpu_ssh_user']
    return environment


def acquire_lock(path, *, shared=False):
    # /run/lock may be root-owned and sticky. Its root-owned lock file cannot
    # be unlinked by the reviewer; non-sticky writable ancestors are rejected.
    trusted_path(path.parent, directory=True, sticky_parents=True)
    info = path.parent.lstat()
    require_root_owned(path.parent, info, sticky_directory=True)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        require_root_owned(path, os.fstat(fd))
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise ValueError('Lock must be a root-owned regular file with one link')
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another hosted AE run is active; retry after it finishes') from error
        return fd
    except BaseException:
        os.close(fd)
        raise


def result_path(policy, selected, caller, *, resume=False, trust=None, allow_root=False):
    root = trusted_path(policy['output_root'], directory=True, trust=trust, root_leaf=True)
    if selected is None:
        raise ValueError('A result destination must be selected')
    path = selected if selected.is_absolute() else root / selected
    if '..' in path.parts or (path == root and not (allow_root or resume)) or not path.is_relative_to(root):
        raise ValueError('Results must stay inside the configured output root')
    for parent in reversed(path.parents):
        if not parent.is_relative_to(root):
            continue
        if not parent.exists() and not parent.is_symlink() and not resume:
            # The runner creates output parents only after acquiring its
            # rotation/read lease. Admission itself must not race a backup.
            continue
        trusted_path(parent, directory=True, trust=trust)
    if resume:
        trusted_tree(path, trust=trust, resume_controls=True)
        # Control files must never be links, even when input payload links are
        # retained in a result. No reviewer-writable plan is accepted on resume.
        for name in ('review.json', 'SUMMARY.md'):
            trusted_path(path / name, trust=trust, root_leaf=True)
    elif (path.exists() or path.is_symlink()) and not (allow_root and path == root):
        raise ValueError('Output already exists; choose a new path or use --resume')
    return path


def default_result(policy, args, *, trust=None):
    # Source identity belongs in the measurement record, not its directory name.
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    if args.quick_check:
        return Path('checks') / ('quick-check-' + stamp)
    if args.max_events is not None or args.limit is not None or args.experiment or args.group:
        return Path('selected') / stamp
    return Path('.')


def command_line(policy, args, output):
    command = [str(policy['python']), '-I', str(policy['runtime_root'] / 'ae/scripts/run_review.py'),
               '--config', str(policy['config'])]
    for key, flag in (('all', '--all'), ('quick_check', '--test'), ('list', '--list')):
        if getattr(args, key):
            command.append(flag)
    for key in ('experiment', 'group'):
        for value in getattr(args, key) or []:
            command += ['--' + key, value]
    if args.baseline_inputs is not None:
        command += ['--baseline-inputs', args.baseline_inputs]
    for key in ('limit', 'max_events', 'numa_node', 'cpus'):
        if getattr(args, key) is not None:
            command += ['--' + key.replace('_', '-'), str(getattr(args, key))]
    if output is not None:
        command += ['--resume' if args.resume else '--output', str(output)]
    return command


def audit_launch(policy, caller, command, *, trust=None):
    path = policy['output_root'].parent / '.launcher-audit.jsonl'
    if path.exists() or path.is_symlink():
        trusted_path(path, trust=trust, root_leaf=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        require_root_owned(path, os.fstat(stream.fileno()))
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('Audit file must have one link')
        stream.write(json.dumps(dict(started_at=datetime.now(timezone.utc).isoformat(),
                                     caller_uid=caller.pw_uid, caller=caller.pw_name, command=command)) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    lock_fd = None
    try:
        policy = load_policy()
        caller = caller_identity(policy)
        trust = runtime_trust(policy)
        args = parse_arguments(argv)
        runtime = trusted_path(policy['runtime_root'], directory=True, trust=trust)
        if args.checkout.resolve(strict=True) != runtime:
            raise ValueError('Checkout differs from the fixed hosted runtime')
        trusted_path(policy['config'])
        trusted_path(policy['python'], symlinks=True, trust=trust)
        if 'temporary_root' in policy:
            temporary = policy['temporary_root']
            work = runtime / 'ae/work'
            if temporary == work or not temporary.is_relative_to(work):
                raise ValueError('Temporary files must stay inside runtime ae/work')
            trusted_path(temporary, directory=True, trust=trust, root_leaf=True)
        environment = fixed_environment(policy, caller)
        # Keep the maintenance gate held through cleanup. The trusted runner
        # separately admits one main lane and one isolated quick-check lane.
        lock_fd = acquire_lock(policy['lock_file'], shared=True)
        venv = policy['python'].parent.parent
        environments = (venv,) if (venv / 'pyvenv.cfg').is_file() else ()
        # paper_data.materialize links paper/*/data through traces/objects;
        # imported objects and baseline staging may live under work/results.
        data_roots = tuple(runtime / 'ae' / name for name in ('results', 'work', 'paper', 'traces'))
        seen = set()
        trusted_tree(runtime, code=True, trust=trust, data_roots=data_roots,
                     environment_roots=environments, seen=seen)
        if (venv / 'pyvenv.cfg').is_file():
            trusted_tree(venv, code=True, external_code=True, trust=trust,
                         data_roots=data_roots, seen=seen)
        trusted_path(policy['output_root'], directory=True, trust=trust, root_leaf=True)
        os.umask(0o022)
        selected = args.resume or args.output
        if not args.list and selected is None:
            selected = default_result(policy, args, trust=trust)
        output = None if args.list else result_path(policy, selected, caller,
                                                    resume=bool(args.resume), trust=trust,
                                                    allow_root=not (args.quick_check or args.limit is not None or args.max_events is not None or args.experiment or args.group))
        command = command_line(policy, args, output)
        audit_launch(policy, caller, command, trust=trust)
        print(f'Hosted AE runtime: {runtime}; caller: {caller.pw_name} (uid {caller.pw_uid})', flush=True)
        if output is not None:
            print(f'Hosted AE results: {output}', flush=True)
        os.chdir(runtime)
        # Keep the lock in the runner itself, including during signal cleanup.
        os.set_inheritable(lock_fd, True)
        os.execve(str(policy['python']), command, environment)
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f'Hosted AE refused: {error}', file=sys.stderr)
        return 2
    finally:
        if lock_fd is not None:
            os.close(lock_fd)  # Reached only when exec fails (or during tests).


if __name__ == '__main__':
    raise SystemExit(main())
