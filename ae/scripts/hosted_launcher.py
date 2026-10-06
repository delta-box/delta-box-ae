#!/usr/bin/python3 -I
"""Start a fixed, trusted AE runtime through a restricted sudo entry point.

Install this file as /usr/local/sbin/deltabox-ae-run, owned by root and not
writable by reviewers. The default policy is /etc/deltabox-ae/launcher.json;
an optional root-owned launchers.json registers policies for other checkouts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import fcntl
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import struct
import sys
import subprocess
import time
import uuid

CPU_STOP_GRACE = 700  # Existing lane cleanup may wait 600 seconds.
CPU_UNIT_PATTERN = r'deltabox-ae-cpu-[0-9a-f]{32}\.service'
CPU_REVIEWER_YIELD = 125
CPU_SYSTEMD_RUNTIME = Path('/run/systemd/system')
CPU_NUMA_DROPIN = '60-deltabox-numa.conf'
CPU_LAYOUT_PROPERTIES = {
    'numa12': {'AllowedCPUs': '24-71', 'AllowedMemoryNodes': '1-2',
               'CPUAffinity': '32-35', 'NUMAPolicy': 'bind', 'NUMAMask': '1'},
    'numa03': {'AllowedCPUs': '0-23 72-95', 'AllowedMemoryNodes': '0 3',
               'CPUAffinity': '4-7', 'NUMAPolicy': 'bind', 'NUMAMask': '0'},
}


class ReviewerYield(Exception):
    """Only the background unit yields; reviewer work is never signalled."""

POLICY_PATH = Path('/etc/deltabox-ae/launcher.json')
POLICY_REGISTRY_PATH = Path('/etc/deltabox-ae/launchers.json')
POLICY_FIELDS = {'runtime_root', 'python', 'config', 'environment_file',
                 'output_root', 'allowed_user', 'lock_file'}
OPTIONAL_POLICY_FIELDS = {'trusted_maintainer', 'trusted_developer', 'temporary_root', 'results_backup_root', 'gpu_ssh_user', 'coordination_root'}
EXPERIMENTS = ('table-02-deltabox', 'table-03-slow', 'table-02-replay',
               'table-02-criu', 'table-02-fc-diff', 'table-02-cube', 'table-02-e2b',
               'figure-02-filesystem', 'figure-02-memory',
               'figure-06-memory', 'figure-06-adaptive', 'figure-08-deltabox',
               'figure-08-cube', 'figure-08-e2b', 'figure-09', 'correctness', 'figure-08-gpu')
GROUPS = ('deltabox', 'baselines', 'table-02', 'table-03', 'figure-02',
          'figure-06', 'figure-08', 'figure-08-cpu', 'gpu', 'cpu', 'figure-09', 'correctness')
GPU_CASES = tuple(f'{phase}-B{batch}' for phase in ('generation', 'training') for batch in (1, 4, 16, 64))
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


def registered_policy_path(checkout):
    """Select only a fixed root-owned registration; never accept a policy flag.

    Parent launchers and owned CPU services use the same --checkout argument.
    Unknown checkouts still face the default policy's runtime identity check.
    lstat distinguishes a missing registry from a dangling or forbidden link.
    """
    try:
        POLICY_REGISTRY_PATH.lstat()
    except FileNotFoundError:
        return POLICY_PATH, False
    registry = read_root_json(POLICY_REGISTRY_PATH)
    for runtime, policy_path in registry.items():
        for value in (runtime, policy_path):
            if (not isinstance(value, str) or not value or '\x00' in value
                    or not Path(value).is_absolute() or '..' in Path(value).parts
                    or str(Path(value)) != value):
                raise ValueError('Launcher registrations require canonical absolute checkout and policy paths')
    selected = registry.get(str(checkout))
    return (Path(selected), True) if selected is not None else (POLICY_PATH, False)


def load_policy(checkout):
    path, registered = registered_policy_path(checkout)
    policy = read_root_json(path)
    if (not POLICY_FIELDS <= set(policy) or set(policy) - POLICY_FIELDS - OPTIONAL_POLICY_FIELDS
            or any(not isinstance(value, str) or not value for value in policy.values())):
        raise ValueError('Launcher policy requires: ' + ', '.join(sorted(POLICY_FIELDS)) +
                         '; optional: ' + ', '.join(sorted(OPTIONAL_POLICY_FIELDS)))
    for key in (POLICY_FIELDS - {'allowed_user'}) | ({'temporary_root', 'results_backup_root', 'coordination_root'} & set(policy)):
        path = Path(policy[key])
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError(f'Policy {key} must be an absolute path without parent traversal')
        policy[key] = path
    if registered and policy['runtime_root'] != checkout:
        raise ValueError('Registered policy differs from its exact checkout')
    if 'gpu_ssh_user' in policy:
        account = pwd.getpwnam(policy['gpu_ssh_user'])
        if account.pw_uid == 0:
            raise ValueError('GPU SSH transport must use an unprivileged account')
    return policy


def policy_coordination_root(policy):
    return Path(policy.get('coordination_root', Path(policy['runtime_root']) / 'ae/work'))


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


def gpu_device_selection(value):
    items = value.split(',')
    if (not items or any(item not in tuple(str(i) for i in range(8)) for item in items)
            or len(set(items)) != len(items)):
        raise argparse.ArgumentTypeError('--gpu-devices requires unique physical GPU indices from 0 to 7')
    return [int(item) for item in items]


def gpu_case_selection(value):
    cases = value.split(',')
    if not cases or len(set(cases)) != len(cases) or any(case not in GPU_CASES for case in cases):
        raise argparse.ArgumentTypeError('--gpu-cases requires unique case IDs from ' + ','.join(GPU_CASES))
    return [case for case in GPU_CASES if case in cases]


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
    parser.add_argument('--cpu-parallel', action='store_true', help='Two bounded CPU lanes on NUMA1 and NUMA2')
    parser.add_argument('--cpu-layout', choices=('numa12', 'numa03'), action=Once,
                        help='Fixed CPU layout; numa03 gives hosted reviewers priority')
    parser.add_argument('--e2b-profile', choices=('paper-nested',), action=Once,
                        help='E2B-only documented nested reconstruction; complete original eight inputs')
    parser.add_argument('--cube-profile', choices=('paper-disk',), action=Once,
                        help='Cube-only documented disk/NUMA reconstruction; full twelve inputs')
    parser.add_argument('--gpu-devices', type=gpu_device_selection, action=Once, metavar='ID,...',
                        help='GPU-only physical device allowlist on the fixed remote host')
    parser.add_argument('--gpu-cases', type=gpu_case_selection, action=Once, metavar='CASE,...',
                        help='Explicit GPU-only case selection; default all eight; paper coverage still requires eight')
    parser.add_argument('--baseline-inputs', choices=('44', 'all'), action=Once,
                        help='Replay/CRIU/FC-diff: fixed 44 complete trajectories by default, or all inputs')
    parser.add_argument('--limit', type=positive_integer, action=Once)
    parser.add_argument('--resume-failures', type=int, choices=range(4), action=Once,
                        help='Opt in to at most 3 verified-cleanup resumes of a fresh hosted NUMA1/2 CPU run')
    parser.add_argument('--isolated-validation', action='store_true', help='Small selected VM validation with separate output and explicit placement')
    parser.add_argument('--max-events', type=positive_integer, action=Once)
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--output', type=Path, action=Once, help='New result path, relative to the fixed output root or absolute within it')
    output.add_argument('--resume', type=Path, action=Once, help='Existing result path, relative to the fixed output root or absolute within it')
    parser.add_argument('--reuse-completed-from', type=Path, action=Once,
                        help='Verify completed Figure9, Cube paper-disk or E2B paper-nested inputs; retain their original source')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--numa-node', type=int, action=Once, help='NUMA node for this run; inherited by all selected CPU experiments')
    parser.add_argument('--cpus', action=Once, help='CPU list inside the selected NUMA node')
    args = parser.parse_args(argv)
    args.cpu_layout = args.cpu_layout or 'numa12'
    args.resume_failures = args.resume_failures or 0
    if args.cpu_layout != 'numa12' and not (args.cpu_parallel or args.isolated_validation):
        parser.error('--cpu-layout numa03 requires --cpu-parallel or strict isolated baseline validation')
    if args.cpu_parallel and (args.group != ['cpu'] or args.experiment or args.all or args.quick_check
            or args.numa_node is not None or args.cpus is not None or args.gpu_cases
            or args.cube_profile or args.e2b_profile or args.isolated_validation or args.reuse_completed_from):
        parser.error('--cpu-parallel requires --group cpu with a fixed CPU layout')
    if args.resume_failures and (not args.cpu_parallel or args.cpu_layout != 'numa12'
            or args.list or args.resume or args.output is None):
        parser.error('--resume-failures requires a fresh hosted NUMA1/2 CPU run with --output')
    if args.e2b_profile is not None:
        if (args.experiment != ['table-02-e2b'] or args.group or args.all or args.quick_check
                or args.list or args.limit is not None or args.max_events is not None
                or args.gpu_cases is not None or args.resume is not None
                or (args.reuse_completed_from is not None and args.output is None)
                or args.cube_profile is not None or args.numa_node is not None or args.cpus is not None):
            parser.error('--e2b-profile requires complete explicit table-02-e2b only; no overrides or resume; references require a new output')
    if args.cube_profile is not None:
        if (set(args.experiment or []) != {'table-02-cube'} or args.group or args.all or args.quick_check
                or args.list or args.limit is not None or args.max_events is not None
                or args.gpu_cases is not None or args.resume is not None
                or args.numa_node is not None or args.cpus is not None):
            parser.error('--cube-profile requires complete explicit table-02-cube only; profile controls placement')
    if args.gpu_cases is not None or args.gpu_devices is not None:
        explicit = bool(args.experiment or args.group)
        if (not explicit or set(args.experiment or []) - {'figure-08-gpu'} or set(args.group or []) - {'gpu'}
                or args.all or args.quick_check or args.list or args.limit is not None or args.max_events is not None):
            parser.error('--gpu-cases/--gpu-devices requires explicit GPU-only selection without quick-check or limits')
    if args.quick_check and (args.experiment or args.group or args.limit is not None or args.max_events is not None):
        parser.error('--test already selects one DeltaBox instance and three events')
    if args.all and (args.experiment or args.group):
        parser.error('--all cannot be combined with a selected experiment/group')
    if args.list and (args.output or args.resume):
        parser.error('--list does not create or resume results')
    if args.reuse_completed_from is not None:
        selected_figure09 = 'figure-09' in (args.experiment or []) or 'figure-09' in (args.group or [])
        selected_cube = (args.experiment == ['table-02-cube'] and not args.group
                         and args.cube_profile == 'paper-disk')
        selected_e2b = (args.experiment == ['table-02-e2b'] and not args.group
                        and args.e2b_profile == 'paper-nested')
        if (not (selected_figure09 or selected_cube or selected_e2b) or args.quick_check or args.all or args.resume or args.list
                or args.limit is not None or args.max_events is not None):
            parser.error('--reuse-completed-from requires explicit Figure9, Cube paper-disk or E2B paper-nested selection, a new output, and complete inputs')
    if args.numa_node is not None or args.cpus is not None:
        if args.numa_node is None or args.cpus is None:
            parser.error('--numa-node and --cpus must be supplied together')
        if args.numa_node < 0 or not re.fullmatch(r'[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*', args.cpus):
            parser.error('Invalid NUMA/CPU placement')
    if args.isolated_validation and args.cpu_layout == 'numa03':
        limits = {'table-02-criu': 1, 'table-02-fc-diff': 2}
        experiment = args.experiment[0] if len(args.experiment or []) == 1 else None
        if (experiment not in limits or args.group or args.all or args.quick_check
                or args.list or args.cpu_parallel or args.limit != limits[experiment] or args.max_events is not None
                or not (args.output or args.resume) or args.reuse_completed_from or args.gpu_cases
                or args.cube_profile or args.e2b_profile or args.baseline_inputs not in (None, '44')
                or (args.numa_node, args.cpus) not in ((0, '0-3'), (3, '72-75'))):
            parser.error('Isolated NUMA0/3 validation requires CRIU limit1 or FC-Diff limit2, complete inputs, explicit output and exact NUMA0/3 CPUs')
    if args.isolated_validation:
        supported = {'table-02-deltabox', 'table-03-slow', 'figure-02-filesystem',
                     'figure-02-memory', 'figure-06-memory', 'figure-06-adaptive',
                     'figure-09', 'correctness'}
        if args.cpu_layout == 'numa03':
            supported.update(('table-02-criu', 'table-02-fc-diff'))
        if (len(args.experiment or []) != 1 or args.experiment[0] not in supported
                or args.group or args.all or args.quick_check or args.list
                or getattr(args, 'reuse_completed_from', None) or not (args.output or args.resume)
                or args.limit is None or args.limit > 10
                or args.numa_node is None or args.cpus is None):
            parser.error('--isolated-validation requires one supported VM experiment, --limit 1..10, output/resume and paired NUMA/CPUs')
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
    if 'coordination_root' in policy:
        environment['AE_HOSTED_COORDINATION_ROOT'] = str(policy['coordination_root'])
    if 'temporary_root' in policy:
        environment['TMPDIR'] = str(policy['temporary_root'])
    if 'results_backup_root' in policy:
        environment['AE_RESULTS_BACKUP_ROOT'] = str(policy['results_backup_root'])
    if 'gpu_ssh_user' in policy:
        environment['AE_HOSTED_GPU_SSH_USER'] = policy['gpu_ssh_user']
    return environment


def acquire_lock(path, *, shared=False, wait=False):
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
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if wait else fcntl.LOCK_NB))
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
    configured_cap = None
    if policy.get('config'):
        configured_cap = json.loads(Path(policy['config']).read_text()).get('review', {}).get('validation_max_jobs')
    if configured_cap or args.max_events is not None or args.limit is not None or args.experiment or args.group:
        return Path('selected') / stamp
    return Path('.')


def command_line(policy, args, output):
    command = [str(policy['python']), '-I', str(policy['runtime_root'] / 'ae/scripts/run_review.py'),
               '--config', str(policy['config'])]
    for key, flag in (('all', '--all'), ('quick_check', '--test'), ('list', '--list'), ('isolated_validation', '--isolated-validation'), ('cpu_parallel', '--cpu-parallel')):
        if getattr(args, key, False):
            command.append(flag)
    for key in ('experiment', 'group'):
        for value in getattr(args, key) or []:
            command += ['--' + key, value]
    if args.cpu_layout != 'numa12':
        command += ['--cpu-layout', args.cpu_layout]
    if getattr(args, 'e2b_profile', None) is not None:
        command += ['--e2b-profile', args.e2b_profile]
    if getattr(args, 'cube_profile', None) is not None:
        command += ['--cube-profile', args.cube_profile]
    if args.gpu_devices is not None:
        command += ['--gpu-devices', ','.join(map(str, args.gpu_devices))]
    if args.gpu_cases is not None:
        command += ['--gpu-cases', ','.join(args.gpu_cases)]
    if args.baseline_inputs is not None:
        command += ['--baseline-inputs', args.baseline_inputs]
    for key in ('limit', 'max_events', 'numa_node', 'cpus'):
        if getattr(args, key) is not None:
            command += ['--' + key.replace('_', '-'), str(getattr(args, key))]
    if args.reuse_completed_from is not None:
        command += ['--reuse-completed-from', str(args.reuse_completed_from)]
    if output is not None:
        command += ['--resume' if args.resume else '--output', str(output)]
    return command


def audit_launch(policy, caller, command, *, trust=None, event='launch', **identity):
    path = policy['output_root'].parent / '.launcher-audit.jsonl'
    if path.exists() or path.is_symlink():
        trusted_path(path, trust=trust, root_leaf=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        require_root_owned(path, os.fstat(stream.fileno()))
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('Audit file must have one link')
        stream.write(json.dumps(dict(started_at=datetime.now(timezone.utc).isoformat(),
                                     caller_uid=caller.pw_uid, caller=caller.pw_name,
                                     command=command, event=event, **identity)) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def cpu_service_identity(unit, *, cgroup_root=Path('/sys/fs/cgroup'), membership=Path('/proc/self/cgroup')):
    """Bind service admission to its actual cgroup and effective no-swap limit."""
    if not re.fullmatch(CPU_UNIT_PATTERN, unit):
        raise ValueError('Invalid owned CPU service name')
    rows = [line[3:] for line in membership.read_text().splitlines() if line.startswith('0::')]
    if len(rows) != 1:
        raise ValueError('Hosted CPU service requires unified cgroup v2')
    relative = Path(rows[0].lstrip('/'))
    if '..' in relative.parts or relative.name != unit:
        raise ValueError('CPU service does not occupy its owned cgroup')
    group = cgroup_root / relative
    limit = (group / 'memory.swap.max').read_text().strip()
    if limit != '0':
        raise ValueError('CPU service memory.swap.max must be zero')
    return dict(unit=unit, cgroup=str(group), memory_swap_max=limit, main_pid=os.getpid(),
                coverage='Direct AE descendants; external backend services retain their own cleanup')


def cpu_service_command(policy, caller, command, unit):
    if not re.fullmatch(CPU_UNIT_PATTERN, unit):
        raise ValueError('Invalid owned CPU service name')
    layout = cpu_command_layout(command)
    properties = ['--property=' + key + '=' + value
                  for key, value in CPU_LAYOUT_PROPERTIES[layout].items()]
    return ['/usr/bin/systemd-run', '--quiet', '--pipe', '--wait', '--collect',
            '--service-type=exec', '--unit=' + unit,
            '--property=MemoryAccounting=yes', '--property=MemorySwapMax=0',
            '--property=Slice=system.slice',
            '--property=KillMode=mixed', '--property=KillSignal=SIGINT',
            '--property=TimeoutStopSec=' + str(CPU_STOP_GRACE),
            '--property=SendSIGKILL=yes', '--property=UMask=0022',
            '--property=WorkingDirectory=' + str(policy['runtime_root']),
            *properties,
            '--', str(policy['python']), '-I',
            str(policy['runtime_root'] / 'ae/scripts/hosted_cpu_service.py'),
            '--unit', unit, '--caller-uid', str(caller.pw_uid), '--',
            '--checkout', str(policy['runtime_root']), *command[5:]]


def cpu_command_layout(command):
    """Read only the launcher-generated argument vector, never an environment."""
    if '--cpu-layout' not in command:
        return 'numa12'
    layout = command[command.index('--cpu-layout') + 1]
    if layout not in CPU_LAYOUT_PROPERTIES:
        raise ValueError('Unsupported hosted CPU layout')
    return layout


def background_cpu_binding(cgroup):
    """Fail before planning if PID1 did not apply the background constraints."""
    def mask(value):
        result = set()
        for part in value.strip().split(','):
            a, *b = part.split('-')
            result.update(range(int(a), int(b[0]) + 1) if b else [int(a)])
        return result
    group = Path(cgroup)
    cpus = (group / 'cpuset.cpus.effective').read_text().strip()
    mems = (group / 'cpuset.mems.effective').read_text().strip()
    actual = set(os.sched_getaffinity(0))
    policy = dict(line.split(':', 1) for line in subprocess.check_output(
        ['/usr/bin/numactl', '--show'], env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'},
        text=True, stderr=subprocess.PIPE).splitlines() if ':' in line)
    if (mask(cpus) != set(range(24)) | set(range(72, 96)) or mask(mems) != {0, 3}
            or actual != {4, 5, 6, 7} or policy.get('policy', '').strip() != 'bind'
            or policy.get('membind', '').split() != ['0']):
        raise ValueError('Background CPU service actual NUMA0/3 binding differs from admission')
    return dict(cpu_layout='numa03', effective_cpus=cpus, effective_memory_nodes=mems,
                controller_cpus=sorted(actual), controller_membind=0)


def load_cpu_priority(policy, trust):
    source = trusted_path(policy['runtime_root'] / 'ae/scripts/hosted_cpu_priority.py', trust=trust)
    spec = importlib.util.spec_from_file_location('hosted_cpu_priority', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cpu_unit_state(unit, environment):
    if not re.fullmatch(CPU_UNIT_PATTERN, unit):
        raise ValueError('Invalid owned CPU service name')
    result = subprocess.run(['/usr/bin/systemctl', 'show', unit,
        '--property=LoadState,ActiveState,SubState,MainPID,Result,ExecMainStatus,ControlGroup,MemorySwapMax'],
        env=environment, capture_output=True, text=True, timeout=15)
    state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode and state.get('LoadState') != 'not-found':
        raise RuntimeError('Cannot inspect owned CPU service: ' + unit)
    return state


def verify_cpu_service_empty(unit, state, *, cgroup_root=Path('/sys/fs/cgroup')):
    if not re.fullmatch(CPU_UNIT_PATTERN, unit):
        raise ValueError('Invalid owned CPU service name')
    relative = Path('system.slice') / unit
    reported = state.get('ControlGroup')
    if reported and reported != '/' + str(relative):
        raise ValueError('CPU service reports an unexpected cgroup')
    group = cgroup_root / relative
    try:
        events = dict(line.split() for line in (group / 'cgroup.events').read_text().splitlines())
    except FileNotFoundError:
        if group.exists():
            raise RuntimeError('Cannot verify owned CPU cgroup occupancy')
        return dict(cgroup=str(group), cgroup_absent=True)
    if events.get('populated') != '0':
        raise RuntimeError('Owned CPU cgroup still contains processes: ' + unit)
    if state.get('LoadState') == 'not-found':
        try:
            group.rmdir()  # Only this exact, verified-empty collected unit.
        except FileNotFoundError:
            return dict(cgroup=str(group), cgroup_absent=True, cgroup_populated='0',
                        empty_cgroup_removed=False)
        return dict(cgroup=str(group), cgroup_absent=True, cgroup_populated='0',
                    empty_cgroup_removed=True)
    return dict(cgroup=str(group), cgroup_absent=False, cgroup_populated='0')


def stop_cpu_service(unit, environment):
    if not re.fullmatch(CPU_UNIT_PATTERN, unit):
        raise ValueError('Invalid owned CPU service name')
    result = subprocess.run(['/usr/bin/systemctl', 'stop', unit], env=environment,
                            capture_output=True, text=True, timeout=CPU_STOP_GRACE + 30)
    state = cpu_unit_state(unit, environment)
    if state.get('LoadState') != 'not-found' and (
            result.returncode or state.get('ActiveState') not in ('inactive', 'failed')
            or state.get('MainPID') != '0'):
        raise RuntimeError('Owned CPU service did not stop: ' + unit)
    state.update(verify_cpu_service_empty(unit, state))
    return state


def cpu_git_environment(policy, runtime, trust):
    paths = [runtime]
    infra = read_root_json(policy['config']).get('e2b', {}).get('infra')
    if infra:
        # Only the registered, root-approved infra repository is additionally
        # allowed; never import caller Git config or a wildcard allowance.
        infra = trusted_path(infra, directory=True, trust=trust)
        if infra not in paths:
            paths.append(infra)
    result = {'GIT_CONFIG_COUNT': str(len(paths))}
    for index, path in enumerate(paths):
        result['GIT_CONFIG_KEY_' + str(index)] = 'safe.directory'
        result['GIT_CONFIG_VALUE_' + str(index)] = str(path)
    return result


def verify_background_cleanup(policy, command, *, check_experiment_failure=True):
    """Direct-unit emptiness alone cannot prove shared backend restoration."""
    runtime = Path(policy['runtime_root'])
    guard = policy_coordination_root(policy) / 'E2B_SERVICE_RECOVERY_REQUIRED.json'
    if guard.exists():
        raise RuntimeError('E2B backend recovery remains required: ' + str(guard))
    destination = next((command[i + 1] for i, value in enumerate(command[:-1])
                        if value in ('--output', '--resume')), None)
    if destination is None:
        raise ValueError('Background handover requires its fixed output directory')
    output = Path(destination)
    if not output.is_relative_to(runtime / 'ae/results/selected'):
        raise ValueError('Background handover output escaped the fixed results tree')
    # Context guards and failed cleanup receipts survive a killed producer.
    for path in output.rglob('*'):
        if path.name == 'RECOVERY_REQUIRED.json' or (path.is_file() and
                path.name.endswith(('.json',)) and 'cleanup-error' in path.name):
            raise RuntimeError('Backend cleanup requires investigation: ' + str(path))
    for path in [output / 'review.json', *(output / 'lanes').glob('numa*/review.json')]:
        if not path.is_file():
            continue  # A yield may occur before admission creates any record.
        record = json.loads(path.read_text())
        if (check_experiment_failure
                and record.get('concurrency_policy', {}).get('lane') in (
                    'isolated-background-criu-validation', 'isolated-background-fc-diff-validation')
                and (record.get('status') == 'failed'
                     or any(row.get('status') == 'failed' for row in record.get('coverage', [])))):
            raise RuntimeError('A failed baseline diagnostic cannot be retried as a reviewer handover: ' + str(path))
        if any(row.get('cleanup_timeout') for row in record.get('cpu_lanes', {}).values()):
            raise RuntimeError('A CPU lane exceeded its graceful cleanup deadline: ' + str(path))
        if any(step.get('status') == 'failed' and 'cleanup' in step.get('name', '')
               for step in record.get('steps', [])):
            raise RuntimeError('A backend cleanup step failed: ' + str(path))
    queue = output / 'cpu-work-queue.json'
    if check_experiment_failure and queue.is_file() and any(row.get('status') == 'failed'
                              for row in json.loads(queue.read_text()).get('groups', {}).values()):
        raise RuntimeError('A failed experiment cannot be retried as a reviewer handover: ' + str(queue))
    for path in output.rglob('staging-cleanup.json'):
        if json.loads(path.read_text()).get('status') == 'failed':
            raise RuntimeError('Owned experiment staging cleanup failed: ' + str(path))


def retain_backend_recovery(policy, command, error):
    path = policy_coordination_root(policy) / 'CPU_SERVICE_RECOVERY_REQUIRED.json'
    # A failed restoration is persistent admission state, never an automatic
    # retry. The EX-protected reviewer gate consumes this marker after waiting.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, 'w') as stream:
        json.dump({'reason': 'CPU shared backend cleanup could not be verified',
                   'error': f'{type(error).__name__}: {error}', 'command': command,
                   'recorded_at': datetime.now(timezone.utc).isoformat()}, stream, indent=2)
        stream.write('\n')


def begin_background_transaction(policy, unit):
    path = policy_coordination_root(policy) / 'CPU_BACKGROUND_TRANSACTION.json'
    for name in ('CPU_SERVICE_RECOVERY_REQUIRED.json', 'E2B_SERVICE_RECOVERY_REQUIRED.json'):
        guard = path.parent / name
        if guard.exists():
            raise RuntimeError('Shared backend recovery is required before validation: ' + str(guard))
    start_ticks = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
    record = {'pid': os.getpid(), 'start_ticks': start_ticks, 'unit': unit,
              'created_at': datetime.now(timezone.utc).isoformat()}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as stream:
        info = os.fstat(stream.fileno())
        require_root_owned(path, info)
        json.dump(record, stream)
        stream.write('\n')
    return path, (info.st_dev, info.st_ino), record


def finish_background_transaction(transaction, unit):
    path, inode, original = transaction
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
        info = os.fstat(stream.fileno())
        require_root_owned(path, info)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) != inode:
            raise RuntimeError('Background transaction file identity changed: ' + str(path))
        record = json.load(stream)
    if record != original or record.get('pid') != os.getpid() or record.get('unit') != unit:
        raise RuntimeError('Background transaction identity changed; retained: ' + str(path))
    if (path.lstat().st_dev, path.lstat().st_ino) != inode:
        raise RuntimeError('Background transaction inode changed; retained: ' + str(path))
    path.unlink()


def prepare_cpu_numa_dropin(unit, command, record):
    """Persist the exact owned unit policy that systemd 249 omits from its fragment."""
    if os.geteuid() != 0 or not re.fullmatch(CPU_UNIT_PATTERN, unit):
        raise ValueError('CPU NUMA dropin requires a root-owned CPU service')
    properties = CPU_LAYOUT_PROPERTIES[cpu_command_layout(command)]
    if properties['NUMAPolicy'] != 'bind' or not re.fullmatch(r'\d+', properties['NUMAMask']):
        raise ValueError('CPU NUMA dropin requires the fixed hosted bind policy')
    root = trusted_path(CPU_SYSTEMD_RUNTIME, directory=True)
    directory = root / (unit + '.d')
    path = directory / CPU_NUMA_DROPIN
    body = '[Service]\nNUMAPolicy=bind\nNUMAMask=' + properties['NUMAMask'] + '\n'
    record.update(unit=unit, path=str(path), directory=str(directory), content=body,
                  directory_created=False, file_created=False, written='', removed=False,
                  sha256=hashlib.sha256(body.encode()).hexdigest(), bytes=len(body.encode()))
    directory.mkdir(mode=0o755)  # Never adopt an existing directory or dropin.
    info = directory.lstat()
    record.update(directory_created=True, directory_identity=[info.st_dev, info.st_ino])
    require_root_owned(directory, info)
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError('CPU NUMA dropin directory changed before writing')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        info = os.fstat(fd)
        record.update(file_created=True, file_identity=[info.st_dev, info.st_ino])
        require_root_owned(path, info)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('CPU NUMA dropin must be one owned regular file')
        data = body.encode()
        while len(record['written']) < len(data):
            offset = len(record['written'])
            count = os.write(fd, data[offset:])
            if count <= 0:
                raise OSError('Short CPU NUMA dropin write')
            record['written'] += data[offset:offset + count].decode()
        os.fsync(fd)
    finally:
        record['written_sha256'] = hashlib.sha256(record['written'].encode()).hexdigest()
        os.close(fd)


def finish_cpu_numa_dropin(record, unit, state):
    """Remove only our exact files after the existing unit/cgroup cleanup proof."""
    if not record.get('directory_created'):
        return
    expected = CPU_SYSTEMD_RUNTIME / (unit + '.d')
    if (not re.fullmatch(CPU_UNIT_PATTERN, unit) or record['unit'] != unit or
            record['directory'] != str(expected) or record['path'] != str(expected / CPU_NUMA_DROPIN) or
            state.get('cgroup') != str(Path('/sys/fs/cgroup/system.slice') / unit) or
            not (state.get('cgroup_absent') is True or state.get('cgroup_populated') == '0') or
            (state.get('LoadState') != 'not-found' and
             (state.get('ActiveState') not in ('inactive', 'failed') or state.get('MainPID') != '0'))):
        raise RuntimeError('CPU NUMA dropin cleanup lacks the owned empty service proof')
    trusted_path(CPU_SYSTEMD_RUNTIME, directory=True)
    info = expected.lstat()
    require_root_owned(expected, info)
    if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != record['directory_identity']:
        raise RuntimeError('CPU NUMA dropin directory identity changed; retained')
    path = expected / CPU_NUMA_DROPIN
    if set(expected.iterdir()) != ({path} if record['file_created'] else set()):
        raise RuntimeError('CPU NUMA dropin directory contents changed; retained')
    if record['file_created']:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            require_root_owned(path, info)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    [info.st_dev, info.st_ino] != record['file_identity'] or
                    os.read(fd, len(record['content'].encode()) + 1) != record['written'].encode()):
                raise RuntimeError('CPU NUMA dropin file identity or bytes changed; retained')
        finally:
            os.close(fd)
        info = path.lstat()
        if [info.st_dev, info.st_ino] != record['file_identity']:
            raise RuntimeError('CPU NUMA dropin changed before removal; retained')
        path.unlink()
    expected.rmdir()
    record['removed'] = True


def run_cpu_service(policy, caller, command, environment, *, trust=None, yield_requested=None, receipt=None):
    """Keep admission held while systemd owns escaped sessions and stop cleanup."""
    if not Path('/sys/fs/cgroup/cgroup.controllers').is_file():
        raise ValueError('Hosted CPU service requires unified cgroup v2')
    version = subprocess.check_output(['/usr/bin/systemd-run', '--version'], env=environment, text=True)
    match = re.match(r'systemd (\d+)\b', version)
    if not match or int(match[1]) < 240:
        raise ValueError('Hosted CPU service requires systemd 240 or newer')
    unit = 'deltabox-ae-cpu-' + uuid.uuid4().hex + '.service'
    argv = cpu_service_command(policy, caller, command, unit)
    audit_launch(policy, caller, command, trust=trust, event='cpu-service-submitted', unit=unit)
    print('Owned CPU service: ' + unit + '; MemorySwapMax=0', flush=True)
    previous, interrupted = {}, []
    def interrupt(signum, frame):
        interrupted.append(signum)
        raise KeyboardInterrupt(f'CPU launcher interrupted by signal {signum}')
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[signum] = signal.signal(signum, interrupt)
    process, code, workload_code, state, cleanup_error = None, 1, None, {}, None
    transaction, cleanup_verified = None, False
    numa_dropin = {}
    try:
        if yield_requested is not None:
            transaction = begin_background_transaction(policy, unit)
        prepare_cpu_numa_dropin(unit, command, numa_dropin)
        process = subprocess.Popen(argv, env=environment, start_new_session=True)
        if yield_requested is None:
            code = process.wait()
        else:
            while True:
                try:
                    code = process.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if yield_requested():
                        raise ReviewerYield('Hosted reviewer requested admission')
        workload_code = code
        # 125 belongs exclusively to a verified cooperative handover, never
        # to a workload's exit status (which must not trigger an endless retry).
        code = 1 if code == CPU_REVIEWER_YIELD else code if code >= 0 else 128 - code
        state = cpu_unit_state(unit, environment)
        if state.get('LoadState') != 'not-found' and state.get('ActiveState') not in ('inactive', 'failed'):
            state = stop_cpu_service(unit, environment)
            raise RuntimeError('CPU service remained active after systemd-run exited')
        state.update(verify_cpu_service_empty(unit, state))
        if yield_requested is not None:
            try:
                verify_background_cleanup(policy, command, check_experiment_failure=False)
            except BaseException as backend_error:
                retain_backend_recovery(policy, command, backend_error)
                raise
        cleanup_verified = True
        return code
    except BaseException as error:
        yielding = isinstance(error, ReviewerYield)
        code = CPU_REVIEWER_YIELD if yielding else 128 + interrupted[0] if interrupted else 1
        cleanup_error = None if yielding else f'{type(error).__name__}: {error}'
        # Stop the launch client first: it cannot submit a new unit after our
        # stop request. This does not replace stopping the service cgroup.
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        client_error = None
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        except BaseException as error:
            client_error = error
        try:
            state = stop_cpu_service(unit, environment)
        except BaseException as stop_error:
            code = 1
            cleanup_error = f'{type(stop_error).__name__}: {stop_error}'
            if transaction is not None:
                retain_backend_recovery(policy, command, stop_error)
            raise
        if transaction is not None:
            try:
                verify_background_cleanup(policy, command, check_experiment_failure=False)
            except BaseException as backend_error:
                code = 1
                cleanup_error = f'{type(backend_error).__name__}: {backend_error}'
                retain_backend_recovery(policy, command, backend_error)
                raise
        cleanup_verified = True
        if client_error is not None:
            code = 1
            cleanup_error = f'{type(client_error).__name__}: {client_error}'
            raise client_error
        if yielding:
            try:
                verify_background_cleanup(policy, command)
            except BaseException as backend_error:
                code = 1
                cleanup_error = f'{type(backend_error).__name__}: {backend_error}'
                raise
            return CPU_REVIEWER_YIELD
        if interrupted:
            code = 128 + interrupted[0]
            return code
        raise
    finally:
        try:
            try:
                if numa_dropin and cleanup_verified:
                    try:
                        finish_cpu_numa_dropin(numa_dropin, unit, state)
                    except BaseException as dropin_error:
                        code = 1
                        cleanup_error = f'{type(dropin_error).__name__}: {dropin_error}'
                        if transaction is not None:
                            retain_backend_recovery(policy, command, dropin_error)
                        raise
                if transaction is not None and cleanup_verified:
                    try:
                        finish_background_transaction(transaction, unit)
                    except BaseException as commit_error:
                        code = 1
                        cleanup_error = f'{type(commit_error).__name__}: {commit_error}'
                        retain_backend_recovery(policy, command, commit_error)
                        raise
            finally:
                if receipt is not None:
                    receipt.update(unit=unit, returncode=code, workload_returncode=workload_code,
                        cleanup_verified=cleanup_verified, cleanup_error=cleanup_error,
                        interrupted_signals=interrupted, unit_state=state, numa_policy_dropin=numa_dropin)
                audit_launch(policy, caller, command, trust=trust, event='cpu-service-finished',
                             unit=unit, returncode=code, interrupted_signals=interrupted,
                             workload_returncode=workload_code,
                             yielded_to_reviewer=(code == CPU_REVIEWER_YIELD),
                             unit_state=state, cleanup_error=cleanup_error,
                             numa_policy_dropin=numa_dropin)
            if code == CPU_REVIEWER_YIELD:
                print('Reviewer requested admission; owned background unit cleaned and yielded.', flush=True)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def run_background_cpu(policy, caller, args, output, environment, priority, *, trust=None):
    """Retry only cooperative reviewer yields, with the same source and output."""
    while True:
        waiting = priority.reviewer_waiting()
        if waiting:
            print('Waiting for hosted reviewer work before NUMA0/3 validation.', flush=True)
        while waiting:
            time.sleep(0.5)
            waiting = priority.reviewer_waiting()
        # A maintained source tree is held by the caller's shared maintenance
        # lease. Revalidate result controls on each continuation; never mix
        # layouts or silently retry an actual measurement/cleanup failure.
        if (output / 'review.json').is_file():
            result_path(policy, output, caller, resume=True, trust=trust)
            args.resume, args.output = output, None
        command = command_line(policy, args, output)
        code = run_cpu_service(policy, caller, command, environment, trust=trust,
                               yield_requested=priority.reviewer_waiting)
        if code != CPU_REVIEWER_YIELD:
            return code
        print('NUMA0/3 validation will resume completed coverage after reviewer cleanup.', flush=True)



def cpu_campaign_binding(policy):
    """Freeze executable source, protected configuration and the Table2 binary."""
    runtime = Path(policy['runtime_root'])
    spec = importlib.util.spec_from_file_location('cpu_campaign_source_lock', runtime / 'release/lock.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = Path(policy['config'])
    module.git = lambda root, *args: subprocess.check_output(
        ['/usr/bin/git', '-c', 'safe.directory=' + str(root), '-C', str(root), *args], text=True).strip()
    row = {'source': module.runtime_identity(runtime),
           'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
           'launcher_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    binary = json.loads(config.read_text()).get('e2b', {}).get('resume_binary')
    if binary:
        path = Path(binary)
        if not path.is_absolute():
            path = runtime / path
        row['table2_binary'] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return row


def run_cpu_campaign(policy, caller, args, output, environment, *, trust=None):
    """Explicit, bounded failure resumes; successful later attempts never erase failures."""
    binding = cpu_campaign_binding(policy)
    history = {'schema_version': 1, 'max_resumes': args.resume_failures,
               'output': str(output), 'binding': binding, 'attempts': [],
               'scope': 'Fresh start; later verified resumes are reported as resumed, not uninterrupted success'}
    history_path = output / 'cpu-resume-history.json'

    def save_history():
        # The result tree is validated by result_path under the held maintenance
        # and reviewer leases. Never follow or overwrite an existing foreign file.
        trusted_path(output, directory=True, trust=trust)
        if history_path.exists() or history_path.is_symlink():
            trusted_path(history_path, trust=trust, root_leaf=True)
        temporary = history_path.with_name('.cpu-resume-history-' + uuid.uuid4().hex + '.json')
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644), 'w') as stream:
            json.dump(history, stream, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, history_path)

    for index in range(args.resume_failures + 1):
        if cpu_campaign_binding(policy) != binding:
            raise RuntimeError('CPU campaign source/configuration/binary changed; resume refused')
        if index:
            result_path(policy, output, caller, resume=True, trust=trust)
            args.resume, args.output = output, None
        command = command_line(policy, args, output)
        receipt = {}
        code = run_cpu_service(policy, caller, command, environment, trust=trust, receipt=receipt)
        # Exceptions and signals from service/cleanup never enter the retry path.
        row = {'index': index + 1, 'command': command, 'returncode': code,
               'finished_at': datetime.now(timezone.utc).isoformat(), 'service': receipt,
               'decision': 'stopped', 'records': {}}
        history['attempts'].append(row)
        if not output.is_dir():
            audit_launch(policy, caller, command, trust=trust, event='cpu-campaign-no-output', campaign_record=row)
            return code or 1
        # Preserve overwritten control records BEFORE the existing resume path
        # archives failed jobs and their original attempt-specific logs.
        paths = [output / 'review.json', output / 'cpu-work-queue.json',
                 *(output / 'lanes').glob('numa*/review.json')]
        for path in paths:
            if path.is_file():
                raw = path.read_bytes()
                row['records'][str(path.relative_to(output))] = {
                    'sha256': hashlib.sha256(raw).hexdigest(), 'value': json.loads(raw)}
        history['resumed_after_failure'] = index > 0
        save_history()
        clean = (receipt.get('cleanup_verified') is True and not receipt.get('cleanup_error')
                 and not receipt.get('interrupted_signals')
                 and receipt.get('numa_policy_dropin', {}).get('removed') is True)
        review = row['records'].get('review.json', {}).get('value', {})
        if not clean:
            row['decision'] = 'cleanup-unverified'; save_history(); return code or 1
        try:
            for name in ('CPU_SERVICE_RECOVERY_REQUIRED.json', 'E2B_SERVICE_RECOVERY_REQUIRED.json',
                         'CPU_BACKGROUND_TRANSACTION.json'):
                if (policy_coordination_root(policy) / name).exists():
                    raise RuntimeError('Shared backend admission remains blocked: ' + name)
            verify_background_cleanup(policy, command, check_experiment_failure=False)
        except BaseException as error:
            row.update(decision='cleanup-unverified', cleanup_error=type(error).__name__ + ': ' + str(error))
            save_history()
            retain_backend_recovery(policy, command, error)
            raise
        if code == 0:
            row['decision'] = 'completed'; save_history(); return 0
        if code != 1 or receipt.get('workload_returncode') != 1 or review.get('status') != 'failed':
            row['decision'] = 'not-a-retryable-workload-failure'; save_history(); return code
        if index == args.resume_failures:
            row['decision'] = 'resume-budget-exhausted'; save_history(); return code
        row['decision'] = 'resume-after-verified-cleanup'; save_history()
        audit_launch(policy, caller, command, trust=trust, event='cpu-campaign-resume',
                     failed_returncode=code, completed_attempt=index + 1, history=str(history_path))
        print('CPU attempt failed with rc=1; original records retained. Cleanup verified; '
              f'resuming the same output ({index + 1}/{args.resume_failures}).', flush=True)
    raise AssertionError('Unreachable campaign state')

def managed_cpu_execution(args):
    if args.cpu_parallel or (args.isolated_validation and args.cpu_layout == 'numa03'):
        return True
    # The local Figure 8 E2B observer requires an admitted, owned CPU unit
    # for selected serial runs as well as the full parallel CPU campaign.
    if args.quick_check or args.list:
        return False
    return (not (args.experiment or args.group)
            or 'figure-08-e2b' in (args.experiment or [])
            or bool({'cpu', 'figure-08', 'figure-08-cpu'} & set(args.group or [])))


def main(argv=None, *, service_context=None):
    lock_fd = priority_fd = None
    try:
        args = parse_arguments(argv)
        policy = load_policy(args.checkout)
        if service_context is None:
            caller = caller_identity(policy)
        else:
            if os.geteuid() != 0 or os.getuid() != 0:
                raise ValueError('CPU service admission requires root')
            uid, unit = service_context
            allowed = pwd.getpwnam(policy['allowed_user'])
            if uid not in (0, allowed.pw_uid):
                raise ValueError('CPU service caller is not allowed')
            caller = pwd.getpwuid(uid)
        trust = runtime_trust(policy)
        if service_context is not None and (not managed_cpu_execution(args) or args.list):
            raise ValueError('CPU service admission requires a CPU execution')
        runtime = trusted_path(policy['runtime_root'], directory=True, trust=trust)
        if args.checkout.resolve(strict=True) != runtime:
            raise ValueError('Checkout differs from the fixed hosted runtime')
        trusted_path(policy['config'])
        trusted_path(policy['python'], symlinks=True, trust=trust)
        if 'coordination_root' in policy:
            trusted_path(policy['coordination_root'], directory=True, trust=trust)
        if 'temporary_root' in policy:
            temporary = policy['temporary_root']
            work = runtime / 'ae/work'
            if temporary == work or not temporary.is_relative_to(work):
                raise ValueError('Temporary files must stay inside runtime ae/work')
            trusted_path(temporary, directory=True, trust=trust, root_leaf=True)
        environment = fixed_environment(policy, caller)
        # Keep the maintenance gate held through cleanup. The runner uses
        # exclusive run admission with the official serial configuration.
        lock_fd = acquire_lock(policy['lock_file'], shared=True, wait=True)
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
        priority = None
        if not args.list:
            priority = load_cpu_priority(policy, trust)
            if args.cpu_layout != 'numa03':
                # The service independently takes this lease too, so caller
                # SIGKILL cannot make reviewer admission disappear early.
                priority_fd = priority.acquire_reviewer()
        trusted_path(policy['output_root'], directory=True, trust=trust, root_leaf=True)
        os.umask(0o022)
        selected = args.resume or args.output
        if not args.list and selected is None:
            selected = default_result(policy, args, trust=trust)
        output = None if args.list else result_path(policy, selected, caller,
                                                    resume=bool(args.resume), trust=trust,
                                                    allow_root=not (args.quick_check or args.limit is not None or args.max_events is not None or args.experiment or args.group))
        if args.reuse_completed_from is not None:
            source = result_path(policy, args.reuse_completed_from, caller, resume=True, trust=trust)
            if source == output or output.is_relative_to(source) or source.is_relative_to(output):
                raise ValueError('Reused and new result directories must be separate')
            args.reuse_completed_from = source
        command = command_line(policy, args, output)
        executable = str(policy['python'])
        if service_context is not None:
            identity = cpu_service_identity(unit)
            if args.cpu_layout == 'numa03':
                identity.update(background_cpu_binding(identity['cgroup']))
                if args.isolated_validation:
                    # The protected controller starts on CPU4-7. A strict
                    # serial diagnostic must first select its permitted lane,
                    # as the parallel lane launcher already does.
                    command = ['/usr/bin/numactl', '--all', '--physcpubind=' + args.cpus,
                               '--membind=' + str(args.numa_node), *command]
                    executable = command[0]
            if not args.cpu_parallel and args.cpu_layout == 'numa12':
                # The service controller starts on auxiliary CPUs32-35.
                # Serial run_review must see both allowed nodes before its
                # existing pinner selects and validates measurement CPUs.
                command = ['/usr/bin/numactl', '--all', '--physcpubind=24-71', *command]
                executable = command[0]
            # HOME=/root also preserves root-owned dependency Git allowances.
            environment.update(cpu_git_environment(policy, runtime, trust))
            audit_launch(policy, caller, command, trust=trust, event='cpu-service-running', **identity)
        elif not (managed_cpu_execution(args) and not args.list):
            audit_launch(policy, caller, command, trust=trust)
        print(f'Hosted AE runtime: {runtime}; caller: {caller.pw_name} (uid {caller.pw_uid})', flush=True)
        if output is not None:
            print(f'Hosted AE results: {output}', flush=True)
        os.chdir(runtime)
        if managed_cpu_execution(args) and not args.list and service_context is None:
            if args.cpu_layout == 'numa03':
                return run_background_cpu(policy, caller, args, output, environment, priority, trust=trust)
            if args.resume_failures:
                return run_cpu_campaign(policy, caller, args, output, environment, trust=trust)
            return run_cpu_service(policy, caller, command, environment, trust=trust)
        # Keep the lock in the runner itself, including during signal cleanup.
        os.set_inheritable(lock_fd, True)
        if priority_fd is not None:
            os.set_inheritable(priority_fd, True)
        os.execve(executable, command, environment)
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f'Hosted AE refused: {error}', file=sys.stderr)
        return 2
    finally:
        if priority_fd is not None:
            os.close(priority_fd)
        if lock_fd is not None:
            os.close(lock_fd)  # Reached only when exec fails (or during tests).


if __name__ == '__main__':
    raise SystemExit(main())
