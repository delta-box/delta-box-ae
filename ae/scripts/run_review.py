#!/usr/bin/env python3
"""Run selected AE groups within configured job limits, preserving raw failures and results."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import hashlib
import shutil
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'ae'))
from ae.repro.coordination import coordination_root
from repro.catalog import EXPERIMENTS as CPU_EXPERIMENTS
from repro.review_gpu import GPU, FANOUT, finish_gpu
from repro.result_storage import (DEFAULT_BACKUP_ROOT, prepare_latest, run_lock, timestamp,
                                 parallel_run_locks, parallel_output, measurement_phase, no_symlink_parents, output_tree_lock)

EXPERIMENTS = {**CPU_EXPERIMENTS, GPU: 'Figure 8(b) GPU generation and training'}
SKIPPED = []
GPU_CASES = tuple(f'{phase}-B{batch}' for phase in ('generation', 'training') for batch in (1, 4, 16, 64))
from repro.common import (configured_path, configured_value, file_record, host_state,
                          install_termination_handler, load_config, public_config,
                          repository_state, write_json)
from repro.process import execute
from vendor.finalbench.fc_diff_dm.fc_capacity import job_size_gib
from release.lock import from_environment

GROUPS = {
    'table-03': ['table-02-deltabox', 'table-03-slow'],
    'deltabox': ['table-02-deltabox', 'table-03-slow'],
    'baselines': [name for name in EXPERIMENTS if name.startswith('table-02-') and name != 'table-02-deltabox'],
    'table-02': [name for name in EXPERIMENTS if name.startswith('table-02-')],
    'figure-02': ['figure-02-filesystem', 'figure-02-memory'],
    'figure-06': ['figure-06-memory', 'figure-06-adaptive'],
    'figure-08': [*FANOUT, GPU],
    'figure-08-cpu': list(FANOUT),
    'gpu': [GPU],
    'cpu': list(CPU_EXPERIMENTS),
    'figure-09': ['figure-09'],
    'correctness': ['correctness'],
}


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


class GPUCases(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f'{option_string} may be supplied only once')
        setattr(namespace, self.dest, value)


def validate_gpu_selection(args):
    if (getattr(args, 'gpu_cases', None) is None
            and getattr(args, 'gpu_devices', None) is None):
        return
    explicit = bool(args.experiment or args.group)
    if (not explicit or set(args.experiment or []) - {GPU} or set(args.group or []) - {'gpu'}
            or args.all or args.quick_check or args.available or args.list or args.analyze_existing
            or args.limit is not None or args.max_events is not None
            or args.execute_plan or args.probe_plan or args.publish_output):
        raise ValueError('--gpu-cases/--gpu-devices requires explicit GPU-only selection without quick-check, limits or analysis-only modes')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    selection = p.add_mutually_exclusive_group()
    selection.add_argument('--available', action='store_true', help='Explicit partial run for self-built/debug environments; report missing prerequisites')
    selection.add_argument('--all', action='store_true', help='Require all CPU and GPU experiments (default)')
    selection.add_argument('--test', dest='quick_check', action='store_true', help='Quick check: one DeltaBox instance and three checkpoint/restore events')
    selection.add_argument('--smoke', dest='quick_check', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--experiment', action='append', choices=EXPERIMENTS, help='Select an experiment; repeatable')
    p.add_argument('--group', action='append', choices=GROUPS, help='Select a paper/backend group; repeatable')
    p.add_argument('--cpu-parallel', action='store_true', help='Two bounded CPU lanes using the fixed CPU layout')
    p.add_argument('--cpu-layout', choices=('numa12', 'numa03'), default='numa12',
                   help='CPU parallel layout: numa12 for reviewers (default); numa03 for the background CPU run')
    p.add_argument('--cube-profile', choices=('paper-disk',), help='Cube-only documented disk/NUMA reconstruction')
    p.add_argument('--e2b-profile', choices=('paper-nested',), action=GPUCases, help='E2B-only documented nested reconstruction; original eight complete inputs')
    p.add_argument('--gpu-devices', type=gpu_device_selection, action=GPUCases, metavar='ID,...',
                   help='GPU-only physical device allowlist on the configured remote host; never falls back outside it')
    p.add_argument('--gpu-cases', type=gpu_case_selection, action=GPUCases, metavar='CASE,...',
                   help='Explicit GPU-only case selection; default all eight; paper coverage still requires eight')
    p.add_argument('--config', type=Path, default=Path(os.environ.get('AE_CONFIG', REPO / 'ae/configs/spr4numa-review.json')))
    p.add_argument('--experiment-config', action='append', default=[], metavar='EXPERIMENT=PATH', help='Use a separate JSON config for this experiment')
    p.add_argument('--output', type=Path, help='Explicit new output directory; default full run rotates ae/results after verified backup')
    p.add_argument('--resume', type=Path, metavar='RUN_DIR', help='Resume in place: verify configuration and result artifacts; record the current source')
    p.add_argument('--reuse-completed-from', type=Path, metavar='RUN_DIR',
                   help='Explicit Figure 9 completed-job import into a new output; retains original source identities')
    p.add_argument('--baseline-inputs', choices=('44', 'all'), default='44',
                   help='Replay/CRIU/FC-diff input set: fixed 44 complete trajectories (default), or all original inputs')
    p.add_argument('--limit', type=int, help='First N inputs per experiment; explicitly marked quick-check')
    p.add_argument('--isolated-validation', action='store_true', help='Selected small VM validation in a separate output with explicit NUMA/CPUs')
    p.add_argument('--max-events', type=int, help='Explicit event prefix; units depend on backend')
    p.add_argument('--no-pin', action='store_true', help='Explicitly opt out of NUMA and frequency controls')
    p.add_argument('--numa-node', type=int, default=None, help='NUMA node for this run; default: shared measurement.numa_node configuration')
    p.add_argument('--cpus', help='CPU list within the selected node; default: shared measurement.cpus configuration')
    p.add_argument('--analyze-existing', type=Path, metavar='RUN_DIR', help='Analyze existing fresh runs into a new output; executes no experiments')
    p.add_argument('--list', action='store_true', help='List experiment/group names without running')
    p.add_argument('--execute-plan', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--probe-plan', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--publish-output', type=Path, nargs='+', help=argparse.SUPPRESS)
    return p


def load_runtime_environment(config, experiments):
    """Load explicitly configured service credentials in memory, never into result JSON."""
    if not any(name.endswith('-e2b') for name in experiments) or os.environ.get('E2B_API_KEY'):
        return
    filename = config.get('environment_file')
    if not filename:
        return
    path = Path(filename)
    if not path.is_absolute():
        path = Path(config['_config_dir']) / path
    try:
        raw = path.read_text()
    except PermissionError:
        process = subprocess.run(['sudo', '-n', 'cat', str(path)], capture_output=True, text=True, timeout=15)
        if process.returncode:
            raise ValueError('Cannot read configured service environment file: ' + str(path))
        raw = process.stdout
    values = json.loads(raw)
    allowed = {'E2B_API_KEY', 'E2B_API_URL', 'E2B_SANDBOX_URL', 'E2B_TEMPLATE', 'E2B_TEMPLATE_ID'}
    if not isinstance(values, dict) or any(not isinstance(v, str) for k, v in values.items() if k in allowed):
        raise ValueError('Service environment must contain string values')
    for name in allowed:
        if values.get(name):
            os.environ.setdefault(name, values[name])


def deep_merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = deep_merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def config_identity(config):
    # A managed daemon receives a fresh PID/mount proof for each attempt.
    # Its full file hash remains in the review; it is not a measurement setting.
    identity = copy.deepcopy(config)
    # This chooses which complete jobs to run; each job's mode/command is still
    # checked on resume, and the original configuration file hash is retained.
    identity.pop('figure06_adaptive_arms', None)
    if identity.get('cube', {}).get('profile') == 'paper-disk':
        identity['cube'].pop('disk_manifest', None)
    if identity.get('cube', {}).get('manage_memory_service') is True:
        identity['cube'].pop('memory_manifest', None)
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def working_source():
    return from_environment()


def current_source():
    return from_environment()


def complete_selection(args):
    return not (args.quick_check or args.experiment or args.group or args.limit is not None
                or args.max_events is not None or args.available or args.analyze_existing)


def default_output(args):
    """Latest complete run has a stable path; checks use independent subfolders."""
    if args.analyze_existing:
        return REPO / 'ae/work/analysis' / timestamp()
    root = REPO / 'ae/results'
    if args.quick_check:
        return root / 'checks' / ('quick-check-' + timestamp())
    if not complete_selection(args):
        return root / 'selected' / timestamp()
    return root


def write_effective_config(path, config):
    """Keep execution credentials private and publish a readable review snapshot."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    public = public_config(config)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(config, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    if public == config:
        path.chmod(0o644)
        return path
    # mkstemp publishes the raw execution file with mode 0600, never a
    # briefly world-readable copy. Existing resume hashes still bind it.
    public_path = path.with_name(path.stem + '.public.json')
    write_json(public_path, public)
    public_path.chmod(0o644)
    return public_path


def make_output_accessible(root):
    """Publish owned artifacts, keeping hosted results root-owned and reviewer-read-only."""
    root = Path(root)
    if not root.exists():
        return
    if root.is_symlink():
        raise ValueError('Refusing to change ownership through an output symlink: ' + str(root))
    owner = root.stat()
    privileged = hasattr(os, 'geteuid') and os.geteuid() == 0
    hosted = privileged and 'AE_HOSTED_CALLER_UID' in os.environ
    uid, gid = (0, 0) if hosted else (owner.st_uid, owner.st_gid)
    if privileged and not hosted:
        if os.environ.get('SUDO_UID', '').isdigit() and os.environ.get('SUDO_GID', '').isdigit():
            uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
    # Child roots may be created by sudo under a user-owned review directory.
    if uid == 0 and not hosted:
        for parent in root.parents:
            candidate = parent.stat()
            if candidate.st_uid != 0:
                uid, gid = candidate.st_uid, candidate.st_gid
                break
    def update(path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            return
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            os.chown(path, uid, gid, follow_symlinks=False)
        elif info.st_uid != os.getuid():
            return
        extra = stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if stat.S_ISDIR(info.st_mode) else 0)
        mode = stat.S_IMODE(info.st_mode) | extra
        if hosted:
            mode &= ~(0o022 | stat.S_ISUID | stat.S_ISGID)
        os.chmod(path, mode, follow_symlinks=False)
    update(root)
    for directory, dirs, files in os.walk(root, followlinks=False):
        # The other lane owns this subtree, including its live temporary files.
        if Path(directory) == REPO / 'ae/results':
            dirs[:] = [name for name in dirs if name != 'checks']
        for name in dirs + files:
            update(Path(directory) / name)


def verify_reused_images(manifest, *, cache=None):
    """Do not mix old successes with newly replaced source disks during resume."""
    from replay.provenance import cached_digest
    for name, record in manifest.get('images', {}).items():
        if not isinstance(record, dict) or not record.get('path') or not record.get('sha256'):
            raise ValueError('Resume image lacks a verifiable identity: ' + name)
        path = Path(record['path'])
        # Run manifests have no proof that their hash was computed outside
        # a same-tick timestamp ambiguity window. Let the normal versioned
        # cache establish that proof, then compare content even when every
        # recorded stat field matches. Stable disks are hashed once across
        # all resumed jobs; old/ambiguous cache entries are never aged into
        # trust merely because the resume happens later.
        current = cached_digest(path, cache)
        if current['sha256'] != record['sha256']:
            raise ValueError(f'Resume source image changed: {name} {path}; start a new output')


def prerequisite_failure(log_path, returncode):
    """A probe crash, timeout or privilege error is a failure, not unavailability."""
    if returncode != 2:
        return None
    try:
        result = json.loads(Path(log_path).read_text())
        checks = result['checks']
        if result.get('ok') is not False or not isinstance(checks, list) or not checks:
            return None
        if not all(isinstance(check, dict) and isinstance(check.get('ok'), bool) and 'name' in check and 'detail' in check for check in checks):
            return None
        missing = [f"{check['name']}: {check['detail']}" for check in checks if not check['ok']]
        return missing or None
    except (ValueError, KeyError, OSError):
        return None


def pin_requested(args, config):
    if args.no_pin or args.analyze_existing:
        return False
    if args.numa_node is not None or args.cpus is not None:
        return True
    chosen = config.get('measurement', {}).get('pin', True)
    if not isinstance(chosen, bool):
        raise ValueError('measurement.pin must be a JSON boolean')
    return chosen


def nvme_work_root(config):
    """Optional absolute work directory on the host NVMe for a disk baseline."""
    root = config.get('nvme_work_root')
    if root is None:
        return None
    if config.get('baseline_storage') != 'disk':
        raise ValueError('nvme_work_root requires baseline_storage disk')
    path = Path(root)
    if not path.is_absolute() or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('nvme_work_root must be an absolute path without symlinks')
    disk = Path('/mnt/disk2')
    anchor = path if path.exists() else path.parent
    if not anchor.exists() or anchor.stat().st_dev != disk.stat().st_dev:
        raise ValueError('nvme_work_root must be on /mnt/disk2')
    return path


def measurement_placement(args, config):
    """Resolve runtime placement from the caller/configuration, without host constants."""
    settings = config.get('measurement', {})
    if not isinstance(settings, dict):
        raise ValueError('measurement must be a JSON object')
    node = args.numa_node if args.numa_node is not None else os.environ.get('AE_NUMA_NODE', settings.get('numa_node'))
    cpus = args.cpus or os.environ.get('AE_CPUS', settings.get('cpus'))
    if node is not None:
        if type(node) is not int and not (isinstance(node, str) and re.fullmatch(r'[0-9]+', node)):
            raise ValueError('measurement.numa_node must be a nonnegative integer')
        node = int(node)
        if node < 0:
            raise ValueError('measurement.numa_node must be a nonnegative integer')
    if cpus is not None and (not isinstance(cpus, str) or not re.fullmatch(r'[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*', cpus)):
        raise ValueError('measurement.cpus must be a CPU list within the selected node')
    if pin_requested(args, config) and (node is None or not cpus):
        raise ValueError('Set measurement.numa_node and measurement.cpus in the shared configuration, or supply --numa-node and --cpus')
    return dict(node=node, cpus=cpus)


ISOLATED_VALIDATION_EXPERIMENTS = frozenset((
    'table-02-deltabox', 'table-03-slow', 'figure-02-filesystem',
    'figure-02-memory', 'figure-06-memory', 'figure-06-adaptive',
    'figure-09', 'correctness'))


def isolated_background_baseline(args):
    if not args.isolated_validation or args.cpu_layout != 'numa03':
        return False
    limits = {'table-02-criu': 1, 'table-02-fc-diff': 2}
    experiment = args.experiment[0] if len(args.experiment or []) == 1 else None
    if (experiment not in limits or args.group or args.all or args.quick_check
            or args.available or args.analyze_existing or args.list or args.cpu_parallel
            or args.experiment_config or args.no_pin or args.limit != limits[experiment] or args.max_events is not None
            or not (args.output or args.resume) or args.reuse_completed_from or args.gpu_cases
            or args.cube_profile or args.e2b_profile or args.baseline_inputs != '44'
            or args.execute_plan or args.probe_plan or args.publish_output
            or (args.numa_node, args.cpus) not in ((0, '0-3'), (3, '72-75'))):
        raise ValueError('Isolated NUMA0/3 validation requires CRIU limit1 or FC-Diff limit2, complete inputs, explicit output and exact NUMA0/3 CPUs')
    return True


def validate_isolated_baseline_resume(args, previous, release):
    if not isolated_background_baseline(args):
        return
    request = previous.get('measurement_request') or {}
    lane = 'isolated-background-' + args.experiment[0].removeprefix('table-02-') + '-validation'
    if (previous.get('experiments') != args.experiment
            or (previous.get('concurrency_policy') or {}).get('lane') != lane
            or previous.get('status') == 'ok' or previous.get('release') != release
            or (request.get('node'), request.get('cpus')) != (args.numa_node, args.cpus)):
        raise ValueError('Isolated baseline resume requires this unfinished diagnostic with the same source and binding')


def validation_job_limit(config):
    limit = config.get('review', {}).get('validation_max_jobs')
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 10):
        raise ValueError('review.validation_max_jobs must be an integer in [1, 10]')
    return limit


def apply_validation_defaults(args, config):
    limit = validation_job_limit(config)
    if limit is None or args.quick_check or args.analyze_existing:
        return
    selected = set(args.experiment or [])
    for group in args.group or []:
        selected.update(GROUPS[group])
    # GPU case lists are already bounded and --limit refers to CPU inputs.
    # Do not turn a valid selected-GPU request into an invalid prefix request.
    if args.limit is None and (selected == {GPU} or getattr(args, 'e2b_profile', None)):
        return
    if args.limit is None:
        args.limit = limit
    elif args.limit > limit:
        raise ValueError(f'--limit exceeds the configured validation cap of {limit}')


def bounded_plan_limits(name, config, flags, maximum):
    result = list(flags)
    if maximum is None or '--limit' not in result:
        return result
    arms = (len(config.get('figure06_adaptive_arms', ['standard', 'adaptive'])) if name == 'figure-06-adaptive' else 3 if name == 'figure-09'
            else len(config.get('figure06_memory_policies', ['none', 'skip', 'gc', 'warm']))
            if name == 'figure-06-memory' else 1)
    if maximum < arms:
        raise ValueError('Validation cap cannot cover one complete set of experiment arms')
    offset = result.index('--limit') + 1
    result[offset] = str(min(int(result[offset]), maximum // arms))
    return result


def isolated_validation_output(args, config):
    background = isolated_background_baseline(args)
    supported = ISOLATED_VALIDATION_EXPERIMENTS | ({'table-02-criu', 'table-02-fc-diff'} if background else set())
    if (validation_job_limit(config) is None
            or len(args.experiment or []) != 1 or args.experiment[0] not in supported
            or args.group or args.all or args.quick_check or args.available or args.analyze_existing
            or args.experiment_config or getattr(args, 'reuse_completed_from', None) or args.no_pin
            or args.limit is None or not 1 <= args.limit <= 10
            or args.numa_node is None or not args.cpus or not (args.output or args.resume)):
        raise ValueError('Isolated validation requires one supported VM experiment, at most10 inputs, an explicit output/resume and NUMA/CPU binding')
    output = no_symlink_parents((args.resume or args.output).absolute()).resolve()
    root = (REPO / 'ae/results').resolve()
    if output == root or not output.is_relative_to(root):
        raise ValueError('Isolated validation output must be a child of this repository ae/results')
    parallel_output(root, output, quick=False)
    return output


def check_timeout(config):
    timeout = float(config.get('timeout', 14400))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('config timeout must be positive and finite')
    return timeout


def option(command, name):
    return command[command.index(name) + 1] if name in command else None


def job_unavailable(job, config):
    """Only detect concrete missing inputs; actual execution remains authoritative."""
    reasons = []
    name, command = job['experiment'], job['command']
    def require(path, kind='file'):
        if path is None:
            return
        path = Path(path)
        good = path.is_dir() if kind == 'directory' else path.is_file()
        if not good:
            reasons.append(f'Missing {kind}: {path}')
    def path_setting(key, kind='file'):
        try:
            path = configured_path(config, key)
            require(path, kind)
            return path
        except ValueError as error:
            reasons.append(str(error))
    if name == 'table-02-e2b' and config.get('e2b', {}).get('profile') == 'paper-nested':
        try:
            from ae.scripts.e2b_paper_profile import input_rows
            rows = {row['instance']: row for row in input_rows(config)}
            instance = option(command, '--instance')
            row = rows[instance]
            if (option(command, '--trace') != row['local']
                    or option(command, '--repository-commit') != row['repository_commit']
                    or option(command, '--limit') is not None):
                raise ValueError('Planned E2B paper input/commit differs from frozen manifest')
        except (ValueError, KeyError, OSError) as exc:
            reasons.append('E2B paper static input verification failed: ' + str(exc))
    if name.endswith('-cube') and config.get('cube', {}).get('profile') == 'paper-disk':
        try:
            from runners.cube_disk import verify as verify_cube_disk
            verify_cube_disk(config)
        except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
            reasons.append('Cube disk service verification failed: ' + str(exc))
    from ae.scripts.cube_control_context import metadata_enabled
    deferred_cube = name == 'figure-08-cube' and metadata_enabled(config)
    if name.endswith('-cube') and config.get('baseline_storage') == 'tmpfs' and not deferred_cube:
        try:
            from runners.cube_memory import verify as verify_cube_memory
            verify_cube_memory(config)
        except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
            reasons.append('Cube memory service verification failed: ' + str(exc))
    for flag in ('--kernel', '--base-xfs', '--data-xfs', '--criu-dump-binary'):
        if option(command, flag):
            require(option(command, flag))
    if name == 'table-03-slow':
        planned_profile = option(command, '--checkpoint-profile')
        if config.get('checkpoint_profile') == 'async-incremental-lazy' and planned_profile != 'async-incremental-lazy':
            reasons.append('Table3 lazy restore plan does not match the selected checkpoint profile')
        if planned_profile == 'async-incremental-lazy':
            pinned = option(command, '--criu-dump-binary')
            if not pinned:
                reasons.append('Table3 lazy restore requires a pinned CRIU dump/restore binary')
            elif Path(pinned).is_file():
                try:
                    capabilities = subprocess.check_output([pinned, '--version'],
                        env={**os.environ, 'DELTABOX_CRIU_CAPABILITIES': '1'}, text=True, timeout=10)
                    if not {'exact-parent-v1', 'exact-parent-lazy-v1'} <= set(capabilities.split()):
                        reasons.append('Table3 lazy restore requires exact-parent-lazy-v1 for the pinned dump/restore binary')
                except (OSError, subprocess.SubprocessError) as error:
                    reasons.append('Table3 lazy restore capability probe failed: ' + str(error))
    if name == 'figure-06-memory' and config.get('checkpoint_profile') in ('async-incremental', 'async-incremental-lazy'):
        reasons.append('async-incremental supports standard replay only; supply a runtime-default config for Figure 6')
    if name in ('figure-08-deltabox', 'figure-09', 'correctness'):
        images = path_setting('images_dir', 'directory')
        if images:
            require(images / 'data-tools.xfs')
    payload_job = name.startswith('table-02-') and name != 'table-02-deltabox' or name.startswith('figure-02-') or name == 'figure-01-cube'
    if payload_job or name == 'figure-09':
        payload = path_setting('payload', 'directory')
        instance = option(command, '--instance')
        if name == 'figure-09':
            info = json.loads(Path(option(command, '--actions')).read_text())
            instance = info['instance_id']
            from repro.repositories import select_repository
            try:
                select_repository(config, instance, info['base_commit'], required_files=sorted({edit['file_path'] for edit in info['edits']}))
            except FileNotFoundError as error:
                reasons.append(str(error))
        if payload:
            if name != 'figure-09':
                require(payload / 'repos' / ('swe-bench_' + instance), 'directory')
            if payload_job:
                require(payload / 'index_store' / instance, 'directory')
                require(payload / 'moatless-det-src', 'directory')
    if payload_job:
        venv = path_setting('moatless_venv', 'directory')
        if venv:
            require(venv / 'bin/python')
        test = config.get('baseline_test_runtime', {})
        if test.get('backend', 'none') == 'local-pytest':
            if name.endswith(('-cube', '-e2b')):
                reasons.append('This backend has no verified local-pytest binding')
            require(test.get('python') or '<unset baseline_test_runtime.python>')
    if name == 'table-02-criu' and config.get('criu_bin'):
        path_setting('criu_bin')
    # Reachability is only a prerequisite, never evidence of a working backend.
    for backend in ('cube', 'e2b'):
        if name.endswith('-' + backend) and not (backend == 'e2b' and name == 'table-02-e2b'):
            try:
                url = urlsplit(str(configured_value(config, backend + '.api_url')))
                if url.scheme not in ('http', 'https') or not url.hostname:
                    raise ValueError('Invalid ' + backend + '.api_url')
                with socket.create_connection((url.hostname, url.port or (443 if url.scheme == 'https' else 80)), timeout=2):
                    pass
            except (ValueError, OSError) as error:
                reasons.append(f'{backend} service is unavailable: {error}')
    return reasons


def probe_plan(path):
    plan = json.loads(path.read_text())
    config = load_config(Path(plan['review_config']))
    checks = [{'key': job['key'], 'reasons': job_unavailable(job, config)} for job in plan['jobs']]
    print(json.dumps(checks, indent=2))
    return 0


def execute_review_job(index, job, plan, output, stop_event=None):
    """Each worker owns one producer, cleanup and its distinct output tree."""
    job = copy.deepcopy(job) if stop_event is not None else job
    if stop_event is not None and stop_event.is_set():
        job.update(status='cancelled')
        return job
    from_environment()
    budget = float(job.get('timeout_s', plan['review_timeout']))
    wait = job.get('recorded_wait_s')
    estimate = f'; recorded schedule wait={wait / 60:.2f} min' if wait is not None else ''
    print(f'[{index}/{len(plan["jobs"])}] {job["key"]}{estimate}; timeout={budget / 60:.2f} min', flush=True)
    command = job['command']
    memory = plan.get('memory_measurement')
    nvme = plan.get('nvme_measurement')
    if memory and nvme:
        raise ValueError('A job cannot use both tmpfs and NVMe work directories')
    if memory:
        command = ['unshare', '--mount', '--propagation', 'private', sys.executable,
            str(REPO / 'ae/scripts/run_memory_job.py'), '--suite', str(output),
            '--key', job['key'], '--experiment', job['experiment'],
            '--config', plan['review_config'], '--node', str(memory['node']),
            '--size-gib', str(memory['size_gib']), '--', *command]
    elif nvme:
        command = ['unshare', '--mount', '--propagation', 'private', sys.executable,
            str(REPO / 'ae/scripts/run_nvme_job.py'), '--suite', str(output),
            '--key', job['key'], '--work-root', nvme['root'], '--', *command]
    identity = plan.get('measurement_identity', {})
    # Hosted E2B fanout may own daemon restarts and RAM mounts. Give its
    # service restoration the same cleanup window as the other owned backends.
    e2b_service = plan.get('e2b_managed_service')
    result = execute(command, output / 'logs' / plan.get('attempt', 'attempt-001') / job['key'], cwd=REPO,
                     timeout=budget,
                     env=dict(os.environ, AE_RUN_PURPOSE=job['run_purpose'],
                              AE_MEASUREMENT_IDENTITY=json.dumps(identity)), stop_event=stop_event,
                     termination_grace=300 if memory or nvme or plan.get('cube_managed_metadata') or e2b_service else 30)
    job.update(status=result['status'], process_manifest=str(output / 'logs' / plan.get('attempt', 'attempt-001') / job['key'] / 'process.json'))
    if result['status'] == 'ok':
        # The producer and its owned processes have fully exited. Keep
        # all measured evidence while releasing reconstructable copies.
        from repro.staging_cleanup import cleanup_reconstructable_staging
        job['status'] = 'cleaning'
        try:
            if (plan.get('measurement_identity', {}).get('e2b_profile') == 'paper-nested'
                    or plan.get('e2b_paper_inputs', {}).get('profile') == 'paper-nested'):
                job['staging_cleanup'] = {
                    'status': 'retained', 'reason': 'source-dependent-paper-reconstruction',
                    'retained': 'Complete real requests, responses, transport receipts, logs and staged source are required for reproduction and paired validation'}
            elif memory or nvme:
                report = output / job['key'] / 'staging-cleanup.json'
                job['staging_cleanup'] = json.loads(report.read_text()) if report.exists() else {'status': 'not-applicable'}
            else:
                job['staging_cleanup'] = cleanup_reconstructable_staging(output / job['key'])
            job['status'] = 'ok'
        except Exception as error:
            job.update(status='failed', staging_cleanup=dict(status='failed', error=f'{type(error).__name__}: {error}'))
            print(f'[{job["key"]}] post-measurement staging cleanup failed: {error}', flush=True)
    return job


def execute_plan(path, *, paper_context_ready=False, paper_before_job=None, paper_after_job=None):
    """Run a frozen suite; Replay can reproduce the paper's 16 trace workers."""
    plan = json.loads(path.read_text())
    config = load_config(Path(plan['review_config'])) if plan.get('review_config') else {}
    from ae.scripts.e2b_paper_profile import active as e2b_paper_active
    if e2b_paper_active(config) and not paper_context_ready:
        # run_pinned_measurement already holds this suite's NUMA/frequency lease.
        # The wrapper starts/validates L1 and calls back with its guest config.
        from ae.scripts.e2b_paper_suite import run
        return run(path)
    if (paper_before_job is not None or paper_after_job is not None) and not (
            paper_context_ready and e2b_paper_active(config) and plan.get('workers', 1) == 1):
        raise ValueError('Per-input paper callbacks require the owned serial nested context')
    output = Path(plan['review_output'])
    output.mkdir(parents=True, exist_ok=bool(plan.get('resume_verified') or plan.get('import_verified')))
    manifest = output / 'suite.json'
    workers = plan.get('workers', 1)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
        raise ValueError('workers must be an integer in [1, 16]')
    if workers > 1 and any(j['experiment'] != 'table-02-replay' for j in plan['jobs']):
        raise ValueError('Concurrent trace execution is supported only for paper Replay')
    for job in plan['jobs']:
        if job.get('reused_verified') and job['experiment'] == 'table-02-e2b':
            from ae.repro.e2b_reuse import verify_referenced_job
            verify_referenced_job(job, plan)
        elif job.get('reused_verified') and job.get('execution') == 'copied-completed-measurement':
            if job['experiment'] == 'table-02-cube':
                from ae.repro.cube_reuse import verify_imported_job
            else:
                from ae.repro.figure09_reuse import verify_imported_job
            verify_imported_job(job, plan)
    plan.update(status='running', runtime=repository_state(), host=host_state(), release=from_environment())
    write_json(manifest, plan)
    pending = [(i, job) for i, job in enumerate(plan['jobs'], 1) if not job.get('reused_verified')]
    stop_event = threading.Event()
    try:
        if workers == 1:
            for index, job in pending:
                try:
                    if paper_before_job is not None:
                        paper_before_job(index, job, plan, output)
                    job.update(execute_review_job(index, job, plan, output))
                    if paper_after_job is not None:
                        paper_after_job(index, job, plan, output)
                except Exception as error:
                    if paper_before_job is None and paper_after_job is None:
                        raise
                    job.update(status='failed', paper_context_error=type(error).__name__ + ': ' + str(error))
                except BaseException as error:
                    if paper_before_job is not None or paper_after_job is not None:
                        job.update(status='interrupted', paper_context_error=type(error).__name__ + ': ' + str(error))
                        for later_index, later in pending:
                            if later_index > index:
                                later.update(status='not-run', reason='Interrupted during ' + job['key'])
                    raise
                write_json(manifest, plan)
                if job.get('status') != 'ok':
                    for later_index, later in pending:
                        if later_index > index:
                            later.update(status='not-run', reason='Stopped after ' + job['key'])
                    break
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = []
                try:
                    futures = {pool.submit(execute_review_job, i, job, plan, output, stop_event): job
                               for i, job in pending}
                    for future in as_completed(futures):
                        finished = future.result()
                        futures[future].update(finished)
                        if finished.get('status') != 'ok':
                            stop_event.set()
                        write_json(manifest, plan)
                except BaseException:
                    stop_event.set()
                    for future in futures:
                        future.cancel()
                    raise
    finally:
        plan['status'] = 'ok' if plan['jobs'] and all(j.get('status') == 'ok' for j in plan['jobs']) else 'failed'
        write_json(manifest, plan)
        make_output_accessible(output)
    return 0 if plan['status'] == 'ok' else 1


class Review:
    def __init__(self, args, config, output):
        apply_validation_defaults(args, config)
        validate_gpu_selection(args)
        if getattr(args, 'cpu_parallel', False):
            from ae.scripts.run_cpu_parallel import validate
            validate(args)
        from ae.scripts.cube_paper_profile import validate
        validate(args)
        from ae.scripts.e2b_paper_profile import validate as validate_e2b_profile
        validate_e2b_profile(args)
        self.args, self.config, self.output = args, config, output
        self.python = sys.executable
        self.cli = [self.python, str(REPO / 'ae/reproduce.py')]
        self.privilege = [] if hasattr(os, 'geteuid') and os.geteuid() == 0 else ['sudo', '-n', '-E']
        selected = list(args.experiment or [])
        for group in args.group or []:
            selected += GROUPS[group]
        self.experiments = ['table-02-deltabox'] if args.quick_check else list(dict.fromkeys(selected or EXPERIMENTS))
        if GPU in self.experiments:
            self.experiments = [name for name in self.experiments if name != GPU] + [GPU]
        self.available = args.available
        self.overrides = {}
        for override in args.experiment_config:
            name, sep, path = override.partition('=')
            if not sep or name not in EXPERIMENTS or not path or name in self.overrides:
                raise ValueError('--experiment-config requires a unique EXPERIMENT=PATH')
            if name == GPU:
                raise ValueError('Configure GPU with gpu_remote_config; --experiment-config selects CPU configurations')
            self.overrides[name] = Path(path).resolve()
        self.limits = ['--limit', '1', '--max-events', '3'] if args.quick_check else []
        if not args.quick_check:
            for flag, value in (('--limit', args.limit), ('--max-events', args.max_events)):
                if value is not None:
                    self.limits += [flag, str(value)]
        if getattr(args, 'reuse_completed_from', None):
            cube_reuse = self.experiments == ['table-02-cube'] and getattr(args, 'cube_profile', None) == 'paper-disk'
            e2b_reuse = self.experiments == ['table-02-e2b'] and getattr(args, 'e2b_profile', None) == 'paper-nested'
            if (not ('figure-09' in self.experiments or cube_reuse or e2b_reuse)
                    or args.quick_check or args.available or self.limits):
                raise ValueError('--reuse-completed-from requires complete Figure 9, Cube paper-disk or E2B paper-nested inputs')
            source = args.reuse_completed_from.absolute()
            if output.is_relative_to(source) or source.is_relative_to(output):
                raise ValueError('Reuse source and new output must be disjoint')
        self.attempt = 'attempt-001'
        self.record = dict(schema_version=2, status='running', experiments=self.experiments,
                           selection_mode='available' if self.available else 'required',
                           run_purpose='quick-check' if args.quick_check else 'available-cohorts' if self.available else 'ae-cohorts' if args.baseline_inputs == '44' else 'full-cohorts',
                           baseline_inputs=args.baseline_inputs,
                           input_limit=1 if args.quick_check else args.limit,
                           event_limit=3 if args.quick_check else args.max_events,
                           config=str(args.config.resolve()), pinned=pin_requested(args, config), pin_policy='effective per-experiment measurement.pin; explicit CPU/NUMA flags enable; --no-pin disables',
                           release={} if args.analyze_existing else current_source(), measurement_request=dict(pinned=pin_requested(args, config), node=args.numa_node, cpus=args.cpus, env_node=os.environ.get('AE_NUMA_NODE'), env_cpus=os.environ.get('AE_CPUS')),
                           declared_unavailable=config.get('review', {}).get('declared_unavailable', []), skipped=[], coverage=[], steps=[], started_at=datetime.now(timezone.utc).isoformat())
        self.record['gpu_requested_devices'] = getattr(args, 'gpu_devices', None)
        self.record['gpu_requested_cases'] = list(args.gpu_cases or GPU_CASES) if GPU in self.experiments else []
        self.record['gpu'] = dict(mode='auto', status='skipped', successful_cases=0,
                                  reason='GPU stage not reached or not selected')
        self.record['concurrency_policy'] = dict(
            enabled=config.get('review', {}).get('parallel_quick_check', False),
            lane='quick' if args.quick_check else 'main',
            control_cpus=config.get('review', {}).get('control_cpus'),
            resource_scope='exclusive NUMA and CPU frequency policies during each measurement; exclusive results rotation')
        self.record['validation_max_jobs'] = validation_job_limit(config)
        if getattr(args, 'isolated_validation', False):
            self.record['concurrency_policy'].update(
                enabled=True, lane='isolated-bounded-validation',
                resource_scope='Separate output; shared rotation barrier; exclusive selected NUMA/frequency lease')
        if isolated_background_baseline(args):
            self.record['concurrency_policy'].update(
                lane='isolated-background-' + args.experiment[0].removeprefix('table-02-') + '-validation',
                resource_scope='Separate output; exclusive results/backend admission; hosted reviewer priority; exclusive selected NUMA/frequency lease')
        self.cube_disk_manifest = None
        self.record['cube_profile'] = getattr(args, 'cube_profile', None)
        self.record['e2b_profile'] = getattr(args, 'e2b_profile', None)
        self.cube_memory_manifest = None
        self.cube_placement = None
        self.record['source_policy'] = 'record-only; source edits do not block execution or reporting'
        self.previous_record = {}
        if args.resume:
            previous = json.loads((output / 'review.json').read_text())
            self.previous_record = previous
            validate_isolated_baseline_resume(args, previous, self.record['release'])
            if GPU in self.experiments:
                if previous.get('gpu_requested_devices') != self.record['gpu_requested_devices']:
                    raise ValueError('Resume GPU device selection differs; use the original --gpu-devices choice')
                prior_cases = previous.get('gpu_requested_cases', previous.get('gpu', {}).get('requested_cases', list(GPU_CASES)))
                if prior_cases != self.record['gpu_requested_cases']:
                    raise ValueError('Resume GPU case selection differs; use the original --gpu-cases choice')
            if previous.get('completed_job_reuse'):
                self.record['completed_job_reuse'] = copy.deepcopy(previous['completed_job_reuse'])
            if previous.get('baseline_inputs', 'all') != self.record['baseline_inputs']:
                raise ValueError('Resume baseline input set differs; use the original --baseline-inputs choice')
            if previous.get('cube_profile') != self.record['cube_profile']:
                raise ValueError('Resume Cube profile differs; start a new output')
            if previous.get('measurement_request') != self.record['measurement_request']:
                raise ValueError('Resume NUMA/frequency policy differs; start a new output')
            if previous.get('run_purpose') != self.record['run_purpose']:
                raise ValueError('Resume experiment selection/purpose differs; start a new output')
            if previous.get('experiments') != self.experiments:
                from repro.cpu_work_queue import permits_scope_expansion
                if (getattr(args, 'cpu_work_queue', None) is None
                        or not permits_scope_expansion(previous.get('experiments'), self.experiments, CPU_EXPERIMENTS)):
                    raise ValueError('Resume experiment selection/purpose differs; start a new output')
                self.record['queue_scope_expansion'] = dict(previous=previous['experiments'], candidates=self.experiments)
            self.attempt = f"attempt-{int(previous.get('attempt_number', 1)) + 1:03d}"
            self.record['resumed_review'] = file_record(output / 'review.json')
        self.record.update(attempt=self.attempt, attempt_number=int(self.attempt.split('-')[-1]))
        self.work_queue = getattr(args, 'cpu_work_queue', None)
        if self.work_queue is not None:
            self.record['coverage'] = self.work_queue.completed_rows()
            self.record['cpu_work_queue'] = str(self.work_queue.path)
        if args.analyze_existing:
            self.record['analyzer'] = working_source()
            self.record.update(analysis_only=True, run_purpose='analysis-only', input=str(args.analyze_existing.resolve()))

    def save(self):
        write_json(self.output / 'review.json', self.record)
        if self.experiments == [GPU]:
            from ae.scripts.figure08_remote import gpu_summary_lines
            text = '\n'.join(gpu_summary_lines(self.record, self.output))
            for name in ('SUMMARY.md', 'result.md'):
                (self.output / name).write_text(text)
            return
        lines = ['# DeltaBox AE execution', '', f'Status: **{self.record["status"]}**', '',
                 '| Experiment | Status | Jobs selected / planned | Reason |', '|---|---|---|---|']
        for row in self.record['coverage']:
            reasons = '; '.join(row.get('reasons', [])) or ('See review.json for unavailable jobs' if row.get('unavailable_jobs') else '')
            reasons = reasons.replace('|', '\\|').replace('\n', ' ')
            lines.append(f'| {row["experiment"]} | {row["status"]} | {row.get("available_jobs", 0)} / {row.get("planned_jobs", "?")} | {reasons} |')
        imported = self.record.get('completed_job_reuse')
        if imported:
            old = imported['original_release']
            lines += ['', f"Figure 9 explicitly reuses {imported['reused_jobs']} verified completed jobs from "
                      f"`{old['source_commit']}` (source SHA-256 `{old['source_sha256']}`). "
                      'Their original manifests are retained byte for byte; new jobs use the planner source. '
                      'This is a multi-source campaign; statistical populations remain separate.', '']
        lines += ['', '| Step | Status | Log |', '|---|---|---|']
        for step in self.record['steps']:
            lines.append(f'| {step["name"]} | {step["status"]} | [log]({step["log"]}) |')
        gpu_selected = GPU in self.record['experiments']
        if gpu_selected:
            lines += ['', 'Figure 8(b) uses automatic remote GPU admission; Figure 8(c) is derived when complete fresh CPU/GPU inputs are available. Unavailable jobs are not passes.']
        else:
            lines += ['', 'GPU experiments (Figure 8(b)(c)) were not selected and are outside the scope of this run.']
        lines += ['Only successful fresh manifests and their hash-bound measurements are analyzed.',
                  'Missing panels remain unavailable; archived values never fill a measurement gap.', '']
        comparison_pages = []
        for filename, label in (('README.md', 'English'), ('README-zh.md', '简体中文')):
            path = Path('comparison') / self.attempt / filename
            if (self.output / path).is_file():
                comparison_pages.append(f'[{label}]({path.as_posix()})')
        if comparison_pages:
            lines += ['Paper comparison / 论文对比：' + ' · '.join(comparison_pages), '']
        if gpu_selected:
            from ae.scripts.figure08_remote import report_lines
            lines += report_lines(self.record['gpu'], self.record.get('gpu_output'))
        for name in ('SUMMARY.md', 'result.md'):
            (self.output / name).write_text('\n'.join(lines))

    def print_summary(self):
        if self.experiments == [GPU]:
            print((self.output / 'SUMMARY.md').read_text(), flush=True)
        print(f'{self.record["status"]}: {self.output / "SUMMARY.md"}', flush=True)

    def terminal_error(self, error):
        interrupted = isinstance(error, KeyboardInterrupt)
        state = 'interrupted' if interrupted else 'failed'
        for step in self.record['steps']:
            if step['status'] == 'running' or (interrupted and step.get('error', '').startswith('KeyboardInterrupt:')):
                step.update(status=state, error=f'{type(error).__name__}: {error}')
        for row in self.record['coverage']:
            if row['status'] in ('checking', 'running'):
                row.update(status=state, reasons=[f'{type(error).__name__}: {error}'])
        self.record.update(status=state, terminal_error=f'{type(error).__name__}: {error}',
                           finished_at=datetime.now(timezone.utc).isoformat())
        self.save()
        if self.experiments == [GPU]:
            self.print_summary()

    def step(self, name, command, timeout=14400, *, unavailable_on_failure=False, termination_grace=30):
        control_cpus = self.config.get('review', {}).get('control_cpus')
        if control_cpus and not name.endswith('-run'):
            if not isinstance(control_cpus, str) or not re.fullmatch(r'[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*', control_cpus):
                raise ValueError('Invalid control CPU list')
            requested = set()
            for item in control_cpus.split(','):
                low, _, high = item.partition('-')
                requested.update(range(int(low), int(high or low) + 1))
            allowed = os.sched_getaffinity(0)
            if not requested <= allowed:
                # A caller confined to other CPUs keeps control work off CPUs reserved elsewhere.
                control_cpus = ','.join(map(str, sorted(allowed)[:4]))
            command = ['taskset', '-c', control_cpus, *command]
        log_dir = self.output / 'logs' / self.attempt / name
        item = dict(name=name, status='running', command=list(map(str, command)),
                    log=str((log_dir / 'stdout.log').relative_to(self.output)))
        self.record['steps'].append(item)
        self.save()
        print(f'[{name}] {log_dir / "stdout.log"}', flush=True)
        try:
            result = execute(item['command'], log_dir, cwd=REPO, timeout=timeout, termination_grace=termination_grace)
            item.update(status=result['status'], returncode=result.get('returncode'))
            if result.get('error'):
                item['error'] = result['error']
            if unavailable_on_failure and item['status'] != 'ok':
                reasons = prerequisite_failure(log_dir / 'stdout.log', item.get('returncode'))
                if reasons:
                    item.update(status='unavailable', reasons=reasons)
        except BaseException as error:
            item.update(status='failed', error=f'{type(error).__name__}: {error}')
            raise
        finally:
            self.save()
        print(f'[{name}] {item["status"]}', flush=True)
        return item['status'] == 'ok'

    def read_step(self, name):
        return json.loads((self.output / 'logs' / self.attempt / name / 'stdout.log').read_text())

    def run_experiment(self, name):
        if name == GPU:
            return self.run_gpu()
        source_config = self.overrides.get(name, self.args.config.resolve())
        config = load_config(source_config)
        if name not in self.overrides:
            config = deep_merge(config, config.get('review', {}).get('experiment_overrides', {}).get(name, {}))
        from ae.scripts.cube_paper_profile import effective
        config = effective(config, getattr(self.args, 'cube_profile', None))
        from ae.scripts.e2b_paper_profile import effective as effective_e2b_profile
        config = effective_e2b_profile(config, getattr(self.args, 'e2b_profile', None))
        if self.cube_disk_manifest is not None:
            config['cube']['disk_manifest'] = str(self.cube_disk_manifest)
        config.pop('review', None)
        if getattr(self.args, 'cpu_parallel_lane', False):
            config['replay_workers'] = 1
        if pin_requested(self.args, config):
            placement = measurement_placement(self.args, config)
            config['measurement'] = {**config.get('measurement', {}), 'pin': True,
                                     'numa_node': placement['node'], 'cpus': placement['cpus']}
        if name.endswith('-cube') and self.cube_memory_manifest is not None:
            config.setdefault('cube', {})['memory_manifest'] = str(self.cube_memory_manifest)
            config['measurement'] = {**config.get('measurement', {}), **self.cube_placement}
        if name in ('table-02-replay', 'table-02-criu', 'table-02-fc-diff'):
            config['baseline_inputs'] = self.args.baseline_inputs
        if config.get('e2b', {}).get('execution', 'ssh') == 'local':
            for key in ('storage', 'sandbox_dir', 'gocache', 'gomodcache', 'resume_binary', 'parent_manifest'):
                if config['e2b'].get(key):
                    config['e2b'][key] = str(configured_path(config, 'e2b.' + key).resolve())
        # Preserve the original base for relative paths when serializing overrides.
        for key in ('kernel', 'base_xfs', 'images_dir', 'payload', 'moatless_venv', 'nltk_data', 'criu_bin', 'criu_dump_binary', 'deltafs', 'work_dir', 'vm_work_dir',
                    'cube.sdk', 'cube.phase_log', 'cube.phase_binary', 'cube.memory_manifest', 'cube.disk_manifest', 'cube.disk_workspace', 'e2b.infra', 'e2b.ssh_key', 'e2b.fanout_python', 'baseline_test_runtime.python'):
            mapping = config
            parts = key.split('.')
            for part in parts[:-1]:
                mapping = mapping.get(part, {}) if isinstance(mapping, dict) else {}
            if isinstance(mapping, dict) and mapping.get(parts[-1]) and '$' not in str(mapping[parts[-1]]):
                path = configured_path(config, key)
                # A virtual environment's bin/python normally links to the
                # system executable. Keep that entry point so Python finds
                # its pyvenv.cfg and the installed SDK/test dependencies.
                if key in ('e2b.fanout_python', 'baseline_test_runtime.python'):
                    path = path.parent.resolve() / path.name
                else:
                    path = path.resolve()
                mapping[parts[-1]] = str(path)
        images = config.get('instance_data_images', {})
        if not isinstance(images, dict):
            raise ValueError('instance_data_images must be an instance-to-path mapping')
        for instance, value in list(images.items()):
            if not isinstance(value, str) or not value:
                raise ValueError('instance_data_images paths must be nonempty strings')
            if '$' not in value:
                path = Path(value).expanduser()
                images[instance] = str((path if path.is_absolute() else Path(config['_config_dir']) / path).resolve())
        config_path = self.output / 'configs' / self.attempt / (name + '.json')
        public_config_path = write_effective_config(config_path, config)
        timeout = check_timeout(config)
        select = ['--config', str(config_path), '--experiment', name]
        row = dict(experiment=name, config=str(config_path), config_source=file_record(source_config), effective_config=file_record(config_path), config_sha256=config_identity(config),
                   public_config=file_record(public_config_path),
                   status='checking', reasons=[], unavailable_jobs=[],
                   unavailable_arms=[item for item in self.record['declared_unavailable'] if item['experiment'] == name])
        self.record['coverage'].append(row)
        doctor_ok = self.step(name + '-doctor', [*self.privilege, *self.cli, 'doctor', *select], 120,
                              unavailable_on_failure=self.available)
        if not doctor_ok:
            item = self.record['steps'][-1]
            reasons = prerequisite_failure(self.output / item['log'], item.get('returncode'))
            row.update(status='unavailable' if reasons else 'failed',
                       reasons=reasons or ['Prerequisite probe failed unexpectedly; see doctor log'])
            self.save()
            return
        suite = self.output / 'runs' / name
        plan_limits = bounded_plan_limits(name, config, self.limits, validation_job_limit(self.config))
        if not self.step(name + '-plan', [*self.cli, 'plan', *select, '--output', str(suite), *plan_limits], 600):
            row.update(status='failed', reasons=['Cannot build the cohort plan; see plan log'])
            return
        plan = self.read_step(name + '-plan')
        maximum = validation_job_limit(self.config)
        if maximum is not None and len(plan['jobs']) > maximum:
            raise ValueError('Planner exceeded the configured validation job cap')
        row['validation_limits'] = dict(max_jobs=maximum, plan_flags=plan_limits)
        plan.update(review_config=str(config_path), review_output=str(suite), review_timeout=timeout,
                    effective_config_sha256=config_identity(config), attempt=self.attempt)
        plan_path = self.output / 'plans' / self.attempt / (name + '.json')
        write_json(plan_path, plan)
        if not self.step(name + '-inputs', [*self.privilege, self.python, str(Path(__file__).resolve()), '--probe-plan', str(plan_path)], 600):
            row.update(status='failed', reasons=['Input probe failed; see inputs log'])
            return
        probes = {probe['key']: probe['reasons'] for probe in self.read_step(name + '-inputs')}
        if set(probes) != {job['key'] for job in plan['jobs']}:
            raise ValueError('Input probe omitted planned jobs')
        row['planned_jobs'] = len(plan['jobs'])
        row['unavailable_jobs'] = [dict(key=key, reasons=reason) for key, reason in probes.items() if reason]
        jobs = [job for job in plan['jobs'] if not probes[job['key']]]
        row['available_jobs'] = len(jobs)
        if not jobs:
            row.update(status='unavailable', reasons=sorted({reason for reasons in probes.values() for reason in reasons}))
            self.save()
            return
        if row['unavailable_jobs']:
            # A runnable subset is real complete traces, but not the whole cohort.
            for job in jobs:
                if job['run_purpose'] == 'full-cohort':
                    job['run_purpose'] = 'full-trace'
        plan.update(jobs=jobs, unavailable_jobs=row['unavailable_jobs'])
        if self.args.resume and suite.exists():
            self.prepare_resume(plan, suite, row)
        write_json(plan_path, plan)
        measurement = config.get('measurement', {})
        pinned = pin_requested(self.args, config)
        row['measurement'] = dict(pinned=pinned, **measurement_placement(self.args, config))
        frequency_khz = measurement.get('frequency_khz')
        if frequency_khz is not None and (type(frequency_khz) is not int or frequency_khz <= 0):
            raise ValueError('measurement.frequency_khz must be a positive integer')
        if frequency_khz is not None:
            row['measurement']['frequency_khz'] = frequency_khz
        nvme_root = nvme_work_root(config)
        identity = dict(node=row['measurement']['node'], cpus=row['measurement']['cpus'],
                        frequency_policy=(f'locked-{frequency_khz}-khz' if frequency_khz is not None
                                          else 'maximum-pstate' if pinned else 'uncontrolled'),
                        storage_mode='nvme' if nvme_root else (
                            config.get('vm_storage', 'disk') if name in ('table-02-deltabox', 'table-03-slow', 'figure-06-memory', 'figure-06-adaptive') else
                            config.get('baseline_storage', 'disk')))
        workers = config.get('replay_workers', 1) if name == 'table-02-replay' else 1
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
            raise ValueError('replay_workers must be an integer in [1, 16]')
        if config.get('cube', {}).get('profile') == 'paper-disk':
            identity.update(cube_profile='paper-disk', service_cpus=config['cube']['service_cpus'],
                            frequency_policy_cpus=measurement['policy_cpus'])
        if config.get('e2b', {}).get('profile') == 'paper-nested':
            from ae.scripts.e2b_paper_profile import verify_inputs
            identity.update(e2b_profile='paper-nested', topology='nested',
                            action_worker='fresh-process', guest_vcpus=1,
                            guest_mem_mib=2048, fresh_base_per_input=True)
            plan['e2b_paper_inputs'] = verify_inputs(config)
        identity['trace_workers'] = workers
        plan['workers'] = workers
        plan['measurement_identity'] = identity
        if (name == 'figure-09' or (name == 'table-02-cube' and
                getattr(self.args, 'cube_profile', None) == 'paper-disk') or
                (name == 'table-02-e2b' and getattr(self.args, 'e2b_profile', None) == 'paper-nested')) and getattr(self.args, 'reuse_completed_from', None):
            if name == 'table-02-e2b':
                from ae.repro.e2b_reuse import prepare_reuse
            elif name == 'table-02-cube':
                from ae.repro.cube_reuse import prepare_reuse
            else:
                from ae.repro.figure09_reuse import prepare_reuse
            from ae.repro.result_storage import active_references
            imported = prepare_reuse(plan, self.args.reuse_completed_from, suite, repo=REPO,
                verify_images=lambda value: verify_reused_images(value,
                    cache=self.output / f'.reuse-image-hashes-{os.geteuid()}.json'),
                check_active=active_references)
            row['reused_jobs'] = [job['job'] for job in imported['jobs']]
            row['measurement_sources'] = plan['measurement_sources']
            row['reuse_manifest'] = plan['reuse_manifest']
            self.record['completed_job_reuse'] = dict(
                experiment=name, reused_jobs=imported['reused_jobs'], selected_jobs=len(jobs),
                original_release=imported['original_release'], planner_release=self.record['release'],
                manifest=plan['reuse_manifest'],
                analysis_policy=imported['analysis_policy'])
            self.save()
        if nvme_root:
            if not pinned:
                raise ValueError('NVMe Table 2 measurement requires NUMA/frequency pinning')
            plan['nvme_measurement'] = dict(root=str(nvme_root), node=identity['node'])
        elif (name == 'figure-02-memory' or (name.startswith('table-02-') and name not in ('table-02-deltabox', 'table-02-cube'))) and config.get('baseline_storage') == 'tmpfs':
            if not pinned:
                raise ValueError('Memory-backed measurement requires NUMA/frequency pinning')
            plan['memory_measurement'] = dict(node=identity['node'], size_gib=job_size_gib(name, config))
        from ae.scripts.cube_control_context import metadata_enabled
        plan['cube_managed_metadata'] = name == 'figure-08-cube' and metadata_enabled(config)
        e2b = config.get('e2b', {})
        plan['e2b_managed_service'] = (name == 'figure-08-e2b' and 'AE_HOSTED_CALLER_UID' in os.environ
            and e2b.get('execution', 'ssh') == 'local'
            and e2b.get('api_url', '').rstrip('/') in ('http://127.0.0.1:3100', 'http://localhost:3100')
            and e2b.get('sandbox_url', '').rstrip('/') in ('http://127.0.0.1:3102', 'http://localhost:3102'))
        write_json(plan_path, plan)
        budget = sum(float(job.get('timeout_s', timeout)) + 60 for job in jobs if not job.get('reused_verified')) + 120
        pending = [job for job in jobs if not job.get('reused_verified')]
        row['recorded_wait_s'] = sum(float(job['recorded_wait_s']) for job in pending) if all('recorded_wait_s' in job for job in pending) else None
        row['outer_timeout_s'] = budget
        owned_cleanup = (plan.get('memory_measurement') or plan.get('nvme_measurement')
                         or plan.get('cube_managed_metadata') or getattr(self.args, 'e2b_profile', None)
                         or plan.get('e2b_managed_service'))
        command = [self.python, str(Path(__file__).resolve()), '--execute-plan', str(plan_path)]
        if pinned:
            command = [self.python, str(REPO / 'ae/scripts/run_pinned_measurement.py'),
                       '--node', str(row['measurement']['node']), '--cpus', row['measurement']['cpus'],
                       '--out', str(self.output / 'environment' / self.attempt / name), '--timeout', str(budget),
                       '--stop-grace', '360' if owned_cleanup else '30', '--', *command]
        if pinned and measurement.get('policy_cpus'):
            command[2:2] = ['--policy-cpus', measurement['policy_cpus']]
        if pinned and frequency_khz is not None:
            command[2:2] = ['--frequency-khz', str(frequency_khz)]
        ok = self.step(name + '-run', [*self.privilege, *command], budget + 120,
                       termination_grace=420 if owned_cleanup else 30)
        row['status'] = 'partial' if ok and (row['unavailable_jobs'] or row['unavailable_arms']) else 'ok' if ok else 'failed'
        row['reasons'] += [str(item.get('arm', 'panel')) + ': ' + item['reason'] for item in row['unavailable_arms']]
        if (suite / 'suite.json').is_file():
            finished = json.loads((suite / 'suite.json').read_text())
            row['successful_jobs'] = sum(job.get('status') == 'ok' for job in finished['jobs'])
            row['failed_jobs'] = [job['key'] for job in finished['jobs'] if job.get('status') != 'ok']
        self.save()
        owned_outputs = [suite]
        if pinned:
            owned_outputs.append(self.output / 'environment' / self.attempt / name)
        self.step(name + '-ownership', [*self.privilege, self.python, str(Path(__file__).resolve()), '--publish-output', *map(str, owned_outputs)], 600)

    def prepare_resume(self, plan, suite, row):
        from repro.analysis import Evidence, FreshRun
        old = json.loads((suite / 'suite.json').read_text())
        if old.get('effective_config_sha256') != plan['effective_config_sha256']:
            raise ValueError('Resume effective configuration differs for ' + row['experiment'])
        saved_suite = self.output / 'attempt-history' / self.attempt / row['experiment'] / 'suite.json'
        if not saved_suite.exists():
            write_json(saved_suite, old)
        previous = {job['key']: job for job in old['jobs']}
        claimed = set()
        evidence = Evidence(suite, 'fresh')
        reused, failed_paths = [], []
        for job in plan['jobs']:
            prior = previous.get(job['key'])
            if prior and prior.get('status') == 'ok':
                if prior.get('execution') == 'copied-completed-measurement':
                    from ae.repro.figure09_reuse import verify_imported_job
                    verify_imported_job(prior, old)
                # Generated config paths are attempt-specific; compare their contents above.
                def normalized(command):
                    result = list(command)
                    if '--config' in result:
                        result[result.index('--config') + 1] = '<effective-config>'
                    return result
                if normalized(prior['command']) != normalized(job['command']) or prior['run_purpose'] != job['run_purpose']:
                    raise ValueError('Resume command/purpose differs: ' + job['key'])
                manifests = sorted((suite / job['key']).glob('**/run.json'))
                if not manifests:
                    raise ValueError('Successful job has no producer manifest: ' + job['key'])
                for manifest in manifests:
                    validated = FreshRun(evidence, manifest, claimed)
                    # Producers run under sudo, but resume validation runs as
                    # the caller. Keep its cache/lock in the writable output,
                    # separated by uid, rather than opening root-owned ae/work.
                    verify_reused_images(validated.config,
                        cache=self.output / f'.resume-image-hashes-{os.geteuid()}.json')
                job.update(status='ok', reused_verified=True, process_manifest=prior.get('process_manifest'),
                           measurement_release=copy.deepcopy(prior.get('measurement_release') or old.get('release', {})))
                for field in ('execution', 'measurement_release', 'original_run', 'reuse_origin'):
                    if field in prior:
                        job[field] = copy.deepcopy(prior[field])
                reused.append(job['key'])
            elif (suite / job['key']).exists():
                failed_paths.append(job['key'])
        # Validate every reusable job before moving any previous failed evidence.
        for key in failed_paths:
            saved = self.output / 'failed-attempts' / self.attempt / row['experiment'] / key
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(suite / key), saved)
        for field in ('measurement_sources', 'reuse_manifest'):
            if field in old:
                plan[field] = copy.deepcopy(old[field])
                row[field] = copy.deepcopy(old[field])
        sources = []
        for identity in [*old.get('measurement_sources', []), old.get('release'), plan.get('release')]:
            if identity and identity not in sources:
                sources.append(identity)
        plan['measurement_sources'] = sources
        row['measurement_sources'] = copy.deepcopy(sources)
        plan['resume_verified'] = True
        row['reused_jobs'] = reused

    def analyze(self, source, *, run_subdirs=None):
        analysis_dir = self.output / 'analysis' / self.attempt
        plot_dir = self.output / 'plots' / self.attempt
        has_cpu = any(name in CPU_EXPERIMENTS for name in self.record['experiments'])
        analysis_command = [*self.cli, 'analyze', '--source', 'fresh', '--input', str(source),
                            '--output', str(analysis_dir)]
        for relative in run_subdirs or []:
            analysis_command += ['--run-subdir', str(relative)]
        analyzed = self.step('analyze', analysis_command) if has_cpu else False
        if analyzed:
            if self.step('plot-dependencies', [self.python, '-c', 'import matplotlib']):
                self.step('plot', [*self.cli, 'plot', '--input', str(analysis_dir / 'summary.json'), '--output', str(plot_dir)])
        self.record['outputs'] = dict(analysis=str(analysis_dir), plots=str(plot_dir), comparison=str(self.output / 'comparison' / self.attempt))
        if self.record.get('gpu_output'):
            from ae.scripts.figure08_remote import finish_remote
            figure08 = finish_remote(self, analysis_dir, analyzed)
        else:
            figure08 = finish_gpu(self, analysis_dir, analyzed)
        # The live review continues changing as plotting finishes. Bind plots to
        # an immutable coverage snapshot so their manifest SHA remains valid.
        coverage_path = self.output / 'coverage' / self.attempt / 'review.json'
        snapshot = copy.deepcopy(self.record)
        snapshot['status'] = 'measurement-coverage-snapshot'
        write_json(coverage_path, snapshot)
        self.record['comparison_coverage'] = file_record(coverage_path)
        command = [self.python, str(REPO / 'ae/scripts/build_review_comparison.py'),
                   '--coverage', str(coverage_path), '--output', str(self.output / 'comparison' / self.attempt)]
        if analyzed and (plot_dir / 'plots.json').is_file() and any(step['name'] == 'plot' and step['status'] == 'ok' for step in self.record['steps']):
            command += ['--analysis', str(analysis_dir / 'summary.json'), '--plots', str(plot_dir / 'plots.json')]
        if figure08 is not None:
            command += ['--figure08', str(figure08)]
        self.step('paper-comparison', command)

    def run_gpu(self):
        # Selected CPU groups and smoke checks must not unexpectedly load GPU models.
        selected = GPU in self.experiments
        if self.args.quick_check or self.args.max_events is not None or not selected:
            self.record['gpu']['reason'] = 'Figure 8(b) outside this selected CPU/smoke scope'
            self.save()
            return
        from ae.scripts.figure08_remote import DEFAULT_CONFIG, run_auto
        relative = Path('gpu') / self.attempt
        self.record['gpu_output'] = relative.as_posix()
        self.record['gpu'] = dict(mode='auto', status='running', successful_cases=0, reason='Remote GPU admission and measurement')
        self.save()
        print('[figure-08-gpu] automatic remote admission; log ' + str(self.output / relative / 'ssh.log'), flush=True)
        try:
            config_path = self.config.get('gpu_remote_config')
            if config_path:
                config_path = Path(config_path)
                if not config_path.is_absolute():
                    config_path = Path(self.config['_config_dir']) / config_path
            else:
                config_path = DEFAULT_CONFIG
            options = {}
            if self.record['gpu_requested_devices'] is not None:
                options['device_indices'] = self.record['gpu_requested_devices']
            self.record['gpu'] = run_auto(self.output / relative, config_path,
                                          requested_case_ids=self.record['gpu_requested_cases'], **options)
        except Exception as error:
            self.record['gpu'] = dict(mode='auto', status='failed', successful_cases=0,
                                      reason=f'{type(error).__name__}: {error}')
        gpu = self.record['gpu']
        requested = self.record['gpu_requested_cases']
        gpu.setdefault('expected_cases', len(GPU_CASES))
        gpu.setdefault('requested_cases', requested)
        gpu.setdefault('requested_case_count', len(requested))
        gpu.setdefault('successful_selected_cases', 0)
        gpu.setdefault('selected_status', 'unavailable')
        gpu.setdefault('missing_selected_cases', requested)
        self.record['coverage'].append(dict(experiment=GPU, optional=False,
            status={'complete': 'ok', 'skipped': 'unavailable'}.get(gpu['status'], gpu['status']),
            planned_jobs=len(GPU_CASES), available_jobs=gpu.get('successful_cases', 0),
            successful_jobs=gpu.get('successful_cases', 0), reasons=[gpu.get('reason', '')],
            requested_cases=gpu['requested_cases'], requested_case_count=gpu['requested_case_count'],
            successful_selected_cases=gpu['successful_selected_cases'], selected_status=gpu['selected_status'],
            missing_selected_cases=gpu['missing_selected_cases']))
        self.save()
        print('[figure-08-gpu] ' + self.record['gpu']['status'], flush=True)

    def prepare_cube_service(self, contexts, *, validate_only=False, selected=None):
        if getattr(self.args, 'cube_profile', None) == 'paper-disk':
            from ae.scripts.cube_paper_profile import effective
            config = effective(self.config, self.args.cube_profile)
            if validate_only:
                return
            from ae.scripts.cube_disk_context import disk_service
            self.cube_disk_manifest = contexts.enter_context(disk_service(
                self.output / 'environment' / self.attempt / 'cube-disk',
                workspace=Path(config['cube']['disk_workspace']), node=2,
                cpus=config['cube']['service_cpus'], reserve_gib=10))
            self.record['cube_disk_service'] = file_record(self.cube_disk_manifest)
            self.record['cube_profile_provenance'] = config['cube']['profile_provenance']
            self.save()
            return
        selected = selected if selected is not None else [name for name in self.experiments if name.endswith('-cube')]
        if len(selected) > 1:
            for name in selected:
                self.prepare_cube_service(contexts, validate_only=validate_only, selected=[name])
            return
        if not selected:
            return
        configs = []
        for name in selected:
            config = load_config(self.overrides.get(name, self.args.config.resolve()))
            if name not in self.overrides:
                config = deep_merge(config, config.get('review', {}).get('experiment_overrides', {}).get(name, {}))
            configs.append(config)
        from ae.scripts.cube_control_context import metadata_enabled
        if metadata_enabled(configs[0]):
            if selected != ['figure-08-cube'] or not pin_requested(self.args, configs[0]):
                raise ValueError('Managed metadata is supported only for pinned Figure 8 Cube')
            size = configs[0]['cube'].get('memory_size_gib', 12)
            if type(size) is not int or size < 12:
                raise ValueError('Cube RAM workspace must be at least 12 GiB')
            # Preparation must execute inside the runner, after its NUMA lease.
            measurement_placement(self.args, configs[0])
            return
        managed = [c.get('cube', {}).get('manage_memory_service', False) for c in configs]
        if any(type(value) is not bool for value in managed) or len(set(managed)) != 1:
            raise ValueError('Cube experiments must share one explicit memory service policy')
        if managed[0]:
            placements = {(placement['node'], placement['cpus'])
                          for c in configs for placement in [measurement_placement(self.args, c)]}
            sizes = {c.get('cube', {}).get('memory_size_gib', 16) for c in configs}
            if len(placements) != 1 or len(sizes) != 1 or any(not pin_requested(self.args, c) for c in configs):
                raise ValueError('Managed Cube requires the same pinned NUMA/CPU placement for every experiment')
            if any(type(size) is not int or size < 12 for size in sizes):
                raise ValueError('Cube RAM workspace must be at least 12 GiB')
            node, cpus = placements.pop()
            self.cube_placement = {'numa_node': node, 'cpus': cpus, 'pin': True}
            if validate_only:
                return
            service_out = self.output / 'environment' / self.attempt / selected[0]
            service_options = {}
            control_placement = ('AE_HOSTED_CALLER_UID' in os.environ
                                 or getattr(self.args, 'cpu_layout', 'numa12') == 'numa03')
            if control_placement:
                # Hosted runs own the exclusive results/backend lease. Bind
                # the shared control plane to the actual lane in both layouts,
                # and restore it only after the RAM service is clean.
                # Keep the existing self-managed NUMA0/3 behavior unchanged.
                from ae.scripts.cube_control_context import placement, quiesce_webui, mysql_launcher
                control_out = service_out / 'control-plane'
                guard = control_out / 'RECOVERY_REQUIRED.json'
                contexts.enter_context(quiesce_webui(control_out, guard))
                contexts.enter_context(mysql_launcher(control_out, guard))
                contexts.enter_context(placement(node, cpus, control_out, guard))
                service_options['recovery_guard'] = guard
            from ae.scripts.cube_memory_context import memory_service
            self.cube_memory_manifest = contexts.enter_context(memory_service(
                service_out / 'cube-memory',
                node=node, cpus=cpus, size_gib=sizes.pop(), **service_options))
            if control_placement:
                from ae.scripts.cube_control_context import proof
                proof(node, cpus, control_out / 'placement-active.json')
                self.record['cube_control_plane_placement'] = file_record(control_out / 'placement-active.json')
            self.record['cube_memory_service'] = file_record(self.cube_memory_manifest)
        from runners.cube_memory import verify
        for config in configs:
            if self.cube_memory_manifest is not None:
                config.setdefault('cube', {})['memory_manifest'] = str(self.cube_memory_manifest)
                config['measurement'] = {'numa_node': node, 'cpus': cpus}
            if managed[0] or config.get('baseline_storage') == 'tmpfs':
                verify(config)
        self.save()

    def run(self):
        self.save()
        contexts = ExitStack()
        try:
            from ae.scripts.cube_paper_profile import preparation_lease
            contexts.enter_context(preparation_lease(REPO / 'ae/work', getattr(self.args, 'cube_profile', None)))
            if self.args.analyze_existing:
                source = self.args.analyze_existing.resolve()
                previous = source / 'review.json'
                if previous.is_file():
                    self.record['coverage'] = json.loads(previous.read_text()).get('coverage', [])
                    self.record['experiments'] = json.loads(previous.read_text()).get('experiments', self.experiments)
                    self.record['source_review'] = file_record(previous)
                    self.record['release'] = json.loads(previous.read_text()).get('release', {})
                    self.record['measurement_run_purpose'] = json.loads(previous.read_text()).get('run_purpose')
                    # Analysis-only never opens SSH or starts a new GPU measurement.
                    prior = json.loads(previous.read_text())
                    self.record['gpu'] = prior.get('gpu', self.record['gpu'])
                    if prior.get('gpu_output'):
                        relative = Path('gpu') / 'imported'
                        gpu_source = (source / prior['gpu_output']).resolve()
                        if not gpu_source.is_relative_to(source):
                            raise ValueError('GPU evidence path escapes source review')
                        try:
                            shutil.copytree(gpu_source, self.output / relative)
                            self.record['gpu_output'] = relative.as_posix()
                        except OSError as error:
                            self.record['gpu'] = dict(mode='auto', status='failed', successful_cases=0,
                                                      reason=f'Could not copy prior GPU evidence: {error}')
                self.analyze(source / 'runs' if (source / 'runs').is_dir() else source)
            else:
                load_runtime_environment(self.config, self.experiments)
                # Detect fixed configuration errors before spending hours on earlier suites.
                for selected in self.experiments:
                    chosen = load_config(self.overrides.get(selected, self.args.config.resolve()))
                    if selected not in self.overrides:
                        chosen = deep_merge(chosen, chosen.get('review', {}).get('experiment_overrides', {}).get(selected, {}))
                    if selected == 'table-02-fc-diff' and chosen.get('baseline_storage') == 'tmpfs':
                        job_size_gib(selected, chosen)
                    if (selected == 'table-02-e2b' and 'AE_HOSTED_CALLER_UID' in os.environ
                            and getattr(self.args, 'e2b_profile', None) is None):
                        from repro.staging_cleanup import validate_e2b_storage
                        validate_e2b_storage(chosen)
                if GPU in self.experiments:
                    from ae.scripts.figure08_remote import DEFAULT_CONFIG, load_settings
                    gpu_config = Path(self.config.get('gpu_remote_config', DEFAULT_CONFIG))
                    if not gpu_config.is_absolute():
                        gpu_config = Path(self.config['_config_dir']) / gpu_config
                    load_settings(gpu_config)
                # Validate fixed Cube settings early, but do not retain its RAM
                # copy while unrelated memory-heavy experiments execute.
                self.prepare_cube_service(contexts, validate_only=True)
                # Shared bundles may be imported here. Serialize only preparation,
                # not the subsequent independent VM measurements.
                with run_lock(REPO / 'ae/work/.prepare.lock', wait=True):
                    if not self.step('prepare', [*self.cli, 'prepare']):
                        return 1
                    if not self.step('verify', [self.python, str(REPO / 'ae/scripts/paper_data.py'), 'verify']):
                        return 1
                work = self.work_queue.work() if self.work_queue is not None else self.experiments
                for name in work:
                    try:
                        if name.endswith('-cube'):
                            cube_contexts = ExitStack()
                            try:
                                self.prepare_cube_service(cube_contexts, selected=[name])
                                self.run_experiment(name)
                            finally:
                                try:
                                    cube_contexts.close()
                                except Exception as error:
                                    log = Path('environment') / self.attempt / name / 'cube-cleanup-error.json'
                                    import traceback
                                    write_json(self.output / log, {'error': f'{type(error).__name__}: {error}',
                                        'traceback': ''.join(traceback.format_exception(type(error), error, error.__traceback__))})
                                    self.record['steps'].append(dict(name='cube-service-cleanup', status='failed',
                                        experiment=name, log=str(log), error=f'{type(error).__name__}: {error}'))
                                    raise
                                finally:
                                    self.cube_memory_manifest = None
                                    self.cube_disk_manifest = None
                                    self.cube_placement = None
                        else:
                            self.run_experiment(name)
                    except Exception as error:
                        row = next((row for row in self.record['coverage'] if row['experiment'] == name), None)
                        if row is None:
                            row = dict(experiment=name)
                            self.record['coverage'].append(row)
                        row.update(status='failed', reasons=[f'{type(error).__name__}: {error}'])
                        self.save()
                        print(f'[{name}] failed: {error}', flush=True)
                    row = next((row for row in self.record['coverage'] if row['experiment'] == name), {})
                    if self.work_queue is not None:
                        self.work_queue.finish(name, row)
                    if not row.get('optional') and (row.get('status') == 'failed' or (not self.available and row.get('status') in ('partial', 'unavailable'))):
                        remaining_names = [] if self.work_queue is not None else self.experiments[self.experiments.index(name) + 1:]
                        for remaining in remaining_names:
                            self.record['coverage'].append(dict(experiment=remaining, status='not-run',
                                reasons=['Stopped after failed experiment ' + name]))
                        self.save()
                        return 1
                self.analyze(self.output / 'runs')
        except BaseException as error:
            self.terminal_error(error)
            raise
        finally:
            try:
                contexts.close()
            except Exception as error:
                self.record['steps'].append(dict(name='cube-service-cleanup', status='failed',
                    log='environment/' + self.attempt + ('/cube-disk/' if self.cube_disk_manifest else '/cube-memory/') + 'cleanup-errors.json',
                    error=f'{type(error).__name__}: {error}'))
            failures = any(step['status'] not in ('ok', 'unavailable') for step in self.record['steps'])
            coverage = [row for row in self.record['coverage'] if not row.get('optional')]
            failures |= any(row['status'] == 'failed' for row in coverage) if not self.args.analyze_existing else False
            if not self.available and not self.args.analyze_existing:
                failures |= any(row['status'] in ('partial', 'unavailable') for row in coverage)
            if not self.args.analyze_existing:
                if any(name in CPU_EXPERIMENTS for name in self.experiments):
                    failures |= not any(row.get('successful_jobs', 0) > 0 for row in coverage)
            complete = any(step['name'] == 'paper-comparison' and step['status'] == 'ok' for step in self.record['steps'])
            self.record['status'] = 'interrupted' if self.record['status'] == 'interrupted' else 'failed' if failures or not complete else ('ok-with-unavailable' if any(row['status'] in ('partial', 'unavailable', 'failed') for row in coverage) else 'ok')
            self.record['finished_at'] = datetime.now(timezone.utc).isoformat()
            self.save()
            make_output_accessible(self.output)
        return 1 if self.record['status'] == 'failed' else 0


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        validate_gpu_selection(args)
        if args.cpu_layout != 'numa12' and not (args.cpu_parallel or isolated_background_baseline(args)):
            raise ValueError('--cpu-layout requires --cpu-parallel or strict isolated baseline validation')
        if getattr(args, 'cpu_parallel', False):
            from ae.scripts.run_cpu_parallel import validate
            validate(args)
        from ae.scripts.cube_paper_profile import validate
        validate(args)
        from ae.scripts.e2b_paper_profile import validate as validate_e2b_profile
        validate_e2b_profile(args)
    except ValueError as error:
        p.error(str(error))
    if args.list:
        print(json.dumps({'experiments': EXPERIMENTS, 'groups': GROUPS, 'automatic': {'figure-08-gpu': 'SSH GPU admission on the configured devices; required when selected, reported in result.md'}}, indent=2))
        return 0
    if args.execute_plan:
        return execute_plan(args.execute_plan)
    if args.probe_plan:
        return probe_plan(args.probe_plan)
    if args.publish_output:
        for path in args.publish_output:
            make_output_accessible(path)
        return 0
    if args.quick_check and (args.experiment or args.group or args.limit is not None or args.max_events is not None):
        p.error('--test already selects one DeltaBox instance and three events')
    if args.all and (args.experiment or args.group):
        p.error('--all cannot be combined with a selected experiment/group')
    if any(value is not None and value <= 0 for value in (args.limit, args.max_events)):
        p.error('limits must be positive')
    if not args.analyze_existing and sys.platform != 'linux':
        p.error('Measurements require the Linux AE host; use --analyze-existing to plot copied evidence locally')
    if args.reuse_completed_from and (not args.output or args.resume or args.analyze_existing or args.quick_check
            or args.all or args.available or args.limit is not None or args.max_events is not None):
        p.error('--reuse-completed-from requires a new explicit --output and complete selected experiments')
    if args.resume and (args.output or args.analyze_existing):
        p.error('--resume cannot be combined with --output/--analyze-existing')
    try:
        config = {} if args.analyze_existing else load_config(args.config.resolve())
        apply_validation_defaults(args, config)
        if args.isolated_validation:
            isolated_validation_output(args, config)
            background = isolated_background_baseline(args)
            with run_lock(coordination_root(REPO) / '.results.lock', shared=not background, wait=background):
                return run_selected(args, p)
        if getattr(args, 'cpu_parallel', False):
            from ae.scripts.run_cpu_parallel import run
            with run_lock(coordination_root(REPO) / '.results.lock', wait=True) as lease_fd:
                return run(args, p, config, lease_fd, sys.modules[__name__])
        parallel = config.get('review', {}).get('parallel_quick_check', False)
        if type(parallel) is not bool:
            raise ValueError('review.parallel_quick_check must be a boolean')
        if args.quick_check:
            placement = config.get('review', {}).get('quick_check_measurement', {})
            if args.numa_node is None and args.cpus is None and placement:
                args.numa_node, args.cpus = placement['numa_node'], placement['cpus']
        if parallel:
            if args.no_pin or args.analyze_existing or args.experiment_config:
                raise ValueError('Parallel mode requires configured pinned measurements')
            if args.output is None and args.resume is None:
                args.output = default_output(args)
            output = (args.resume or args.output).absolute()
            parallel_output(REPO / 'ae/results', output, quick=args.quick_check)
            for override in [config, *[deep_merge(config, value) for value in
                    config.get('review', {}).get('experiment_overrides', {}).values()]]:
                if not pin_requested(args, override):
                    raise ValueError('Parallel mode requires pinning for every experiment')
            if args.quick_check:
                if args.numa_node is None or not args.cpus:
                    raise ValueError('Parallel quick check requires an explicit NUMA/CPU placement')
                # Cube may retain its service between measured stages. Do not
                # place the quick VM on that long-lived service's memory node.
                for name, override in config.get('review', {}).get('experiment_overrides', {}).items():
                    if name.endswith('-cube'):
                        cube = deep_merge(config, override)
                        if cube.get('cube', {}).get('manage_memory_service') and args.numa_node == cube.get('measurement', {}).get('numa_node'):
                            raise ValueError('Quick check cannot use the managed Cube NUMA node')
                if config.get('cube', {}).get('manage_memory_service') and args.numa_node == config.get('measurement', {}).get('numa_node'):
                    raise ValueError('Quick check cannot use the managed Cube NUMA node')
            rotate = output.resolve() == (REPO / 'ae/results').resolve() and not args.resume
            with parallel_run_locks(coordination_root(REPO), quick=args.quick_check, rotate=rotate) as gate:
                return run_selected(args, p, gate=gate)
        with run_lock(coordination_root(REPO) / '.results.lock'):
            return run_selected(args, p)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'AE refused: {error}', file=sys.stderr)
        return 2


def run_selected(args, p, *, gate=None):
    config = {} if args.analyze_existing else load_config(args.config.resolve())
    check_timeout(config)
    selected_output = args.resume or args.output or default_output(args)
    if selected_output.is_symlink():
        p.error('Output must not be a symlink')
    output = no_symlink_parents(selected_output).resolve()
    if args.analyze_existing and not args.analyze_existing.is_dir():
        p.error('--analyze-existing must point to an existing directory')
    # Acquire before Review loads resume state or any attempt history is written.
    with output_tree_lock(REPO / 'ae/work', REPO / 'ae/results', output,
                          quick=args.quick_check):
        return run_locked_selection(args, p, config, output, gate=gate)


def assert_backend_recovery(*, background=False):
    # Call only after obtaining the inherited/exclusive results lease. A live
    # background transaction may clear its guard while the reviewer waits.
    guard = coordination_root(REPO) / 'CPU_SERVICE_RECOVERY_REQUIRED.json'
    transaction = coordination_root(REPO) / 'CPU_BACKGROUND_TRANSACTION.json'
    deadline = time.monotonic() + 730
    incomplete_deadline = None
    while transaction.exists():
        if guard.exists():
            raise RuntimeError('Shared backend recovery is required before evaluation: ' + str(guard))
        try:
            with os.fdopen(os.open(transaction, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
                info = os.fstat(stream.fileno())
                if info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
                    raise RuntimeError('Untrusted background transaction: ' + str(transaction))
                record = json.load(stream)
        except FileNotFoundError:
            continue  # The exact owner just committed its cleanup receipt.
        except json.JSONDecodeError as error:
            # O_EXCL publishes an empty trusted file just before its short
            # write. Bound that creation window; persistent damage is failure.
            if incomplete_deadline is None:
                incomplete_deadline = time.monotonic() + 2
            if time.monotonic() >= incomplete_deadline:
                raise RuntimeError('Incomplete background transaction; recovery required: ' + str(transaction)) from error
            time.sleep(0.05)
            continue
        unit = record.get('unit', '')
        membership = Path('/proc/self/cgroup').read_text().splitlines()
        owned = [line[3:].split('/') for line in membership if line.startswith('0::')]
        pid = record.get('pid')
        if type(pid) is not int or pid <= 0:
            raise RuntimeError('Invalid background transaction; recovery required: ' + str(transaction))
        try:
            ticks = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        except (OSError, IndexError):
            ticks = None
        if ticks != record.get('start_ticks') or time.monotonic() >= deadline:
            raise RuntimeError('Unfinished background transaction; recovery required: ' + str(transaction))
        if (background and re.fullmatch(r'deltabox-ae-cpu-[0-9a-f]{32}\.service', unit)
                and any(parts[:3] == ['', 'system.slice', unit] for parts in owned)):
            # Both fixed lanes belong to this transaction. Their service queue
            # may already have created its active E2B guard while the other
            # lane enters; that live guard is owned by this same EX holder.
            return
        # EX may be released by an inner runner slightly before its outer
        # service owner verifies shared recovery and emptiness. Wait for that
        # exact owner to commit the receipt before starting reviewer work.
        time.sleep(0.2)
    if guard.exists():
        raise RuntimeError('Shared backend recovery is required before evaluation: ' + str(guard))
    from ae.scripts.e2b_service_context import assert_backend_ready
    assert_backend_ready()


def run_locked_selection(args, p, config, output, *, gate=None):
    assert_backend_recovery(background=getattr(args, 'cpu_layout', 'numa12') == 'numa03')
    runner = Review(args, config, output)
    latest = output == (REPO / 'ae/results').resolve() and not args.resume
    if latest and not complete_selection(args):
        p.error('Only a complete unrestricted run may replace ae/results; select a child --output')
    backup = None
    if latest:
        destination = Path(os.environ.get('AE_RESULTS_BACKUP_ROOT',
                               config.get('review', {}).get('results_backup_root', str(DEFAULT_BACKUP_ROOT))))
        backup = prepare_latest(output, destination)
        if backup:
            print('Previous results backed up and verified: ' + backup['path'], flush=True)
            runner.record['previous_results_backup'] = backup
    output.mkdir(parents=True, exist_ok=bool(args.resume) or latest)
    if args.resume:
        history = output / 'attempt-history' / runner.attempt
        history.mkdir(parents=True, exist_ok=False)
        for name in ('review.json', 'SUMMARY.md', 'result.md'):
            if (output / name).exists():
                shutil.copy2(output / name, history / name)
    measurement_phase(gate)
    print(f'Output: {output}', flush=True)
    try:
        code = runner.run()
    except KeyboardInterrupt as error:
        runner.terminal_error(error)
        print(f'Interrupted; evidence kept at {output}', file=sys.stderr)
        return 130
    except Exception as error:
        runner.terminal_error(error)
        print(f'AE failed: {error}; evidence kept at {output}', file=sys.stderr)
        return 1
    runner.print_summary()
    return code


if __name__ == '__main__':
    install_termination_handler()
    raise SystemExit(main())
