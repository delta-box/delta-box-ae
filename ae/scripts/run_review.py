#!/usr/bin/env python3
"""Run the complete CPU and GPU AE catalog by default, preserving failures and fresh-only plots."""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
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
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'ae'))
from repro.catalog import EXPERIMENTS as CPU_EXPERIMENTS
from repro.review_gpu import GPU, FANOUT, finish_gpu

EXPERIMENTS = {**CPU_EXPERIMENTS, GPU: 'Figure 8(b) GPU generation and training'}
SKIPPED = []
from repro.common import (configured_path, configured_value, file_record, host_state,
                          install_termination_handler, load_config, public_config,
                          repository_state, write_json)
from repro.process import execute
from vendor.finalbench.fc_diff_dm.fc_capacity import job_size_gib
from release.lock import PATHS as SOURCE_PATHS, fingerprint, from_environment, source_records

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


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    selection = p.add_mutually_exclusive_group()
    selection.add_argument('--available', action='store_true', help='Explicit partial run for self-built/debug environments; report missing prerequisites')
    selection.add_argument('--all', action='store_true', help='Require the CPU catalog with optional automatic remote GPU measurement (default)')
    selection.add_argument('--test', dest='quick_check', action='store_true', help='Quick check: one DeltaBox instance and three checkpoint/restore events')
    selection.add_argument('--smoke', dest='quick_check', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--experiment', action='append', choices=EXPERIMENTS, help='Select an experiment; repeatable')
    p.add_argument('--group', action='append', choices=GROUPS, help='Select a paper/backend group; repeatable')
    p.add_argument('--config', type=Path, default=Path(os.environ.get('AE_CONFIG', REPO / 'ae/configs/spr4numa-review.json')))
    p.add_argument('--experiment-config', action='append', default=[], metavar='EXPERIMENT=PATH', help='Use a separate JSON config for this experiment')
    p.add_argument('--output', type=Path, help='New output directory; never overwritten')
    p.add_argument('--resume', type=Path, metavar='RUN_DIR', help='Resume in place: verify source/config/artifact hashes; retain failed attempts')
    p.add_argument('--baseline-inputs', choices=('44', 'all'), default='44',
                   help='Replay/CRIU/FC-diff input set: fixed 44 complete trajectories (default), or all original inputs')
    p.add_argument('--limit', type=int, help='First N inputs per experiment; explicitly marked quick-check')
    p.add_argument('--max-events', type=int, help='Explicit event prefix; units depend on backend')
    p.add_argument('--no-pin', action='store_true', help='Explicitly opt out of NUMA and frequency controls')
    p.add_argument('--numa-node', type=int, default=None, help='Default: config measurement.numa_node, or 2')
    p.add_argument('--cpus', help='Default: config measurement.cpus, or 52-55')
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
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def working_source():
    records = source_records()
    untracked = subprocess.check_output(['git', '-C', str(REPO), 'ls-files', '--others', '--exclude-standard', '-z', '--', *SOURCE_PATHS], text=True).split('\0')
    for name in untracked:
        if not name or name.endswith('.md'):
            continue
        path = REPO / name
        records[name] = {'symlink': os.readlink(path)} if path.is_symlink() else {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return dict(source_commit=repository_state()['commit'], source_sha256=fingerprint(records), status='unlocked-working-source')


def current_source():
    return from_environment() or working_source()


def result_version(identity):
    commit = identity.get('source_commit', '')
    if isinstance(commit, str) and re.fullmatch('[0-9a-f]{40}', commit):
        return commit[:12]
    digest = identity.get('source_sha256', '')
    if isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest):
        return 'source-' + digest[:12]
    raise ValueError('A versioned result directory requires a source commit or SHA-256')


def default_output(args):
    """Keep one version together; short checks never occupy the full run."""
    if args.analyze_existing:
        review = args.analyze_existing / 'review.json'
        if not review.is_file():
            raise ValueError('Analyzing loose runs requires an explicit --output directory')
        measured = json.loads(review.read_text())['release']
        return REPO / 'ae/results' / result_version(measured) / 'rendering' / result_version(working_source())
    root = REPO / 'ae/results' / result_version(current_source())
    if args.quick_check:
        return root / 'checks/quick-check'
    if args.max_events is not None:
        return root / 'checks/selected'
    return root / 'full'


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
    if name in ('table-02-cube', 'figure-01-cube') and config.get('baseline_storage') == 'tmpfs':
        try:
            from runners.cube_memory import verify as verify_cube_memory
            verify_cube_memory(config)
        except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
            reasons.append('Cube memory service verification failed: ' + str(exc))
    for flag in ('--kernel', '--base-xfs', '--data-xfs', '--criu-dump-binary'):
        if option(command, flag):
            require(option(command, flag))
    if name.startswith('figure-06') and config.get('checkpoint_profile') in ('async-incremental', 'async-incremental-lazy'):
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
    if memory:
        command = ['unshare', '--mount', '--propagation', 'private', sys.executable,
            str(REPO / 'ae/scripts/run_memory_job.py'), '--suite', str(output),
            '--key', job['key'], '--experiment', job['experiment'],
            '--config', plan['review_config'], '--node', str(memory['node']),
            '--size-gib', str(memory['size_gib']), '--', *command]
    identity = plan.get('measurement_identity', {})
    result = execute(command, output / 'logs' / plan.get('attempt', 'attempt-001') / job['key'], cwd=REPO,
                     timeout=budget,
                     env=dict(os.environ, AE_RUN_PURPOSE=job['run_purpose'],
                              AE_MEASUREMENT_IDENTITY=json.dumps(identity)), stop_event=stop_event,
                     termination_grace=300 if memory else 30)
    job.update(status=result['status'], process_manifest=str(output / 'logs' / plan.get('attempt', 'attempt-001') / job['key'] / 'process.json'))
    if result['status'] == 'ok':
        # The producer and its owned processes have fully exited. Keep
        # all measured evidence while releasing reconstructable copies.
        from repro.staging_cleanup import cleanup_reconstructable_staging
        job['status'] = 'cleaning'
        try:
            if memory:
                report = output / job['key'] / 'staging-cleanup.json'
                job['staging_cleanup'] = json.loads(report.read_text()) if report.exists() else {'status': 'not-applicable'}
            else:
                job['staging_cleanup'] = cleanup_reconstructable_staging(output / job['key'])
            job['status'] = 'ok'
        except Exception as error:
            job.update(status='failed', staging_cleanup=dict(status='failed', error=f'{type(error).__name__}: {error}'))
            print(f'[{job["key"]}] post-measurement staging cleanup failed: {error}', flush=True)
    return job


def execute_plan(path):
    """Run a frozen suite; Replay can reproduce the paper's 16 trace workers."""
    plan = json.loads(path.read_text())
    output = Path(plan['review_output'])
    output.mkdir(parents=True, exist_ok=bool(plan.get('resume_verified')))
    manifest = output / 'suite.json'
    workers = plan.get('workers', 1)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
        raise ValueError('workers must be an integer in [1, 16]')
    if workers > 1 and any(j['experiment'] != 'table-02-replay' for j in plan['jobs']):
        raise ValueError('Concurrent trace execution is supported only for paper Replay')
    plan.update(status='running', runtime=repository_state(), host=host_state(), release=from_environment())
    write_json(manifest, plan)
    pending = [(i, job) for i, job in enumerate(plan['jobs'], 1) if not job.get('reused_verified')]
    stop_event = threading.Event()
    try:
        if workers == 1:
            for index, job in pending:
                job.update(execute_review_job(index, job, plan, output))
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
        self.attempt = 'attempt-001'
        self.record = dict(schema_version=2, status='running', experiments=self.experiments,
                           selection_mode='available' if self.available else 'required',
                           run_purpose='quick-check' if self.limits else 'available-cohorts' if self.available else 'ae-cohorts' if args.baseline_inputs == '44' else 'full-cohorts',
                           baseline_inputs=args.baseline_inputs,
                           config=str(args.config.resolve()), pinned=pin_requested(args, config), pin_policy='effective per-experiment measurement.pin; explicit CPU/NUMA flags enable; --no-pin disables',
                           release={} if args.analyze_existing else current_source(), measurement_request=dict(pinned=pin_requested(args, config), node=args.numa_node, cpus=args.cpus, env_node=os.environ.get('AE_NUMA_NODE'), env_cpus=os.environ.get('AE_CPUS')),
                           declared_unavailable=config.get('review', {}).get('declared_unavailable', []), skipped=[], coverage=[], steps=[], started_at=datetime.now(timezone.utc).isoformat())
        self.record['gpu'] = dict(mode='auto', status='skipped', successful_cases=0,
                                  reason='GPU stage not reached or not selected')
        self.previous_record = {}
        if args.resume:
            previous = json.loads((output / 'review.json').read_text())
            self.previous_record = previous
            if previous.get('release', {}).get('source_sha256') != self.record['release']['source_sha256']:
                raise ValueError('Resume source fingerprint differs; start a new output')
            if previous.get('baseline_inputs', 'all') != self.record['baseline_inputs']:
                raise ValueError('Resume baseline input set differs; use the original --baseline-inputs choice')
            if previous.get('measurement_request') != self.record['measurement_request']:
                raise ValueError('Resume NUMA/frequency policy differs; start a new output')
            if previous.get('experiments') != self.experiments or previous.get('run_purpose') != self.record['run_purpose']:
                raise ValueError('Resume experiment selection/purpose differs; start a new output')
            self.attempt = f"attempt-{int(previous.get('attempt_number', 1)) + 1:03d}"
            self.record['resumed_review'] = file_record(output / 'review.json')
        self.record.update(attempt=self.attempt, attempt_number=int(self.attempt.split('-')[-1]))
        if args.analyze_existing:
            self.record['analyzer'] = working_source()
            self.record.update(analysis_only=True, run_purpose='analysis-only', input=str(args.analyze_existing.resolve()))

    def save(self):
        write_json(self.output / 'review.json', self.record)
        lines = ['# DeltaBox AE execution', '', f'Status: **{self.record["status"]}**', '',
                 '| Experiment | Status | Jobs selected / planned | Reason |', '|---|---|---|---|']
        for row in self.record['coverage']:
            reasons = '; '.join(row.get('reasons', [])) or ('See review.json for unavailable jobs' if row.get('unavailable_jobs') else '')
            reasons = reasons.replace('|', '\\|').replace('\n', ' ')
            lines.append(f'| {row["experiment"]} | {row["status"]} | {row.get("available_jobs", 0)} / {row.get("planned_jobs", "?")} | {reasons} |')
        lines += ['', '| Step | Status | Log |', '|---|---|---|']
        for step in self.record['steps']:
            lines.append(f'| {step["name"]} | {step["status"]} | [log]({step["log"]}) |')
        lines += ['', 'Figure 8(b) uses automatic remote GPU admission; Figure 8(c) is derived when complete fresh CPU/GPU inputs are available. Unavailable jobs are not passes.',
                  'Only successful fresh manifests and their hash-bound measurements are analyzed.',
                  'Missing panels remain unavailable; archived values never fill a measurement gap.', '']
        comparison_pages = []
        for filename, label in (('README.md', 'English'), ('README-zh.md', '简体中文')):
            path = Path('comparison') / self.attempt / filename
            if (self.output / path).is_file():
                comparison_pages.append(f'[{label}]({path.as_posix()})')
        if comparison_pages:
            lines += ['Paper comparison / 论文对比：' + ' · '.join(comparison_pages), '']
        from ae.scripts.figure08_remote import report_lines
        lines += report_lines(self.record['gpu'], self.record.get('gpu_output'))
        for name in ('SUMMARY.md', 'result.md'):
            (self.output / name).write_text('\n'.join(lines))

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

    def step(self, name, command, timeout=14400, *, unavailable_on_failure=False, termination_grace=30):
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
        config.pop('review', None)
        if name in ('table-02-replay', 'table-02-criu', 'table-02-fc-diff'):
            config['baseline_inputs'] = self.args.baseline_inputs
        if config.get('e2b', {}).get('execution', 'ssh') == 'local':
            for key in ('storage', 'sandbox_dir', 'gocache', 'gomodcache', 'resume_binary', 'parent_manifest'):
                if config['e2b'].get(key):
                    config['e2b'][key] = str(configured_path(config, 'e2b.' + key).resolve())
        # Preserve the original base for relative paths when serializing overrides.
        for key in ('kernel', 'base_xfs', 'images_dir', 'payload', 'moatless_venv', 'nltk_data', 'criu_bin', 'criu_dump_binary', 'deltafs', 'work_dir', 'vm_work_dir',
                    'cube.sdk', 'cube.phase_log', 'cube.phase_binary', 'e2b.infra', 'e2b.ssh_key', 'e2b.fanout_python', 'baseline_test_runtime.python'):
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
        write_json(config_path, config)
        config_path.chmod(0o600)
        timeout = check_timeout(config)
        select = ['--config', str(config_path), '--experiment', name]
        row = dict(experiment=name, config=str(config_path), config_source=file_record(source_config), effective_config=file_record(config_path), config_sha256=config_identity(config),
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
        if not self.step(name + '-plan', [*self.cli, 'plan', *select, '--output', str(suite), *self.limits], 600):
            row.update(status='failed', reasons=['Cannot build the cohort plan; see plan log'])
            return
        plan = self.read_step(name + '-plan')
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
        row['measurement'] = dict(pinned=pinned, node=self.args.numa_node if self.args.numa_node is not None else int(os.environ.get('AE_NUMA_NODE', measurement.get('numa_node', 2))),
                                  cpus=self.args.cpus or os.environ.get('AE_CPUS', measurement.get('cpus', '52-55')))
        identity = dict(node=row['measurement']['node'], cpus=row['measurement']['cpus'],
                        frequency_policy='maximum-pstate' if pinned else 'uncontrolled',
                        storage_mode=config.get('vm_storage', 'disk') if name in ('table-02-deltabox', 'table-03-slow', 'figure-06-memory', 'figure-06-adaptive') else
                            config.get('baseline_storage', 'disk'))
        workers = config.get('replay_workers', 1) if name == 'table-02-replay' else 1
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
            raise ValueError('replay_workers must be an integer in [1, 16]')
        identity['trace_workers'] = workers
        plan['workers'] = workers
        plan['measurement_identity'] = identity
        if name.startswith('table-02-') and name not in ('table-02-deltabox', 'table-02-cube') and config.get('baseline_storage') == 'tmpfs':
            if not pinned:
                raise ValueError('Memory Table 2 measurement requires NUMA/frequency pinning')
            plan['memory_measurement'] = dict(node=identity['node'], size_gib=job_size_gib(name, config))
        write_json(plan_path, plan)
        budget = sum(float(job.get('timeout_s', timeout)) + 60 for job in jobs if not job.get('reused_verified')) + 120
        pending = [job for job in jobs if not job.get('reused_verified')]
        row['recorded_wait_s'] = sum(float(job['recorded_wait_s']) for job in pending) if all('recorded_wait_s' in job for job in pending) else None
        row['outer_timeout_s'] = budget
        command = [self.python, str(Path(__file__).resolve()), '--execute-plan', str(plan_path)]
        if pinned:
            command = [self.python, str(REPO / 'ae/scripts/run_pinned_measurement.py'),
                       '--node', str(row['measurement']['node']), '--cpus', row['measurement']['cpus'],
                       '--out', str(self.output / 'environment' / self.attempt / name), '--timeout', str(budget),
                       '--stop-grace', '360' if plan.get('memory_measurement') else '30', '--', *command]
        ok = self.step(name + '-run', [*self.privilege, *command], budget + 120,
                       termination_grace=420 if plan.get('memory_measurement') else 30)
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
                job.update(status='ok', reused_verified=True, process_manifest=prior.get('process_manifest'))
                reused.append(job['key'])
            elif (suite / job['key']).exists():
                failed_paths.append(job['key'])
        # Validate every reusable job before moving any previous failed evidence.
        for key in failed_paths:
            saved = self.output / 'failed-attempts' / self.attempt / row['experiment'] / key
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(suite / key), saved)
        plan['resume_verified'] = True
        row['reused_jobs'] = reused

    def analyze(self, source):
        analysis_dir = self.output / 'analysis' / self.attempt
        plot_dir = self.output / 'plots' / self.attempt
        has_cpu = any(name in CPU_EXPERIMENTS for name in self.record['experiments'])
        analyzed = self.step('analyze', [*self.cli, 'analyze', '--source', 'fresh', '--input', str(source),
                                         '--output', str(analysis_dir)]) if has_cpu else False
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
            self.record['gpu'] = run_auto(self.output / relative, config_path)
        except Exception as error:
            self.record['gpu'] = dict(mode='auto', status='failed', successful_cases=0,
                                      reason=f'{type(error).__name__}: {error}')
        gpu = self.record['gpu']
        self.record['coverage'].append(dict(experiment=GPU, optional=True,
            status={'complete': 'ok', 'skipped': 'unavailable'}.get(gpu['status'], gpu['status']),
            planned_jobs=8, available_jobs=gpu.get('successful_cases', 0),
            successful_jobs=gpu.get('successful_cases', 0), reasons=[gpu.get('reason', '')]))
        self.save()
        print('[figure-08-gpu] ' + self.record['gpu']['status'], flush=True)

    def run(self):
        self.save()
        try:
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
                    if selected == 'table-02-e2b' and 'AE_HOSTED_CALLER_UID' in os.environ:
                        from repro.staging_cleanup import validate_e2b_storage
                        validate_e2b_storage(chosen)
                if not self.step('prepare', [*self.cli, 'prepare']):
                    return 1
                if not self.step('verify', [self.python, str(REPO / 'ae/scripts/paper_data.py'), 'verify']):
                    return 1
                for name in self.experiments:
                    try:
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
                    if not row.get('optional') and (row.get('status') == 'failed' or (not self.available and row.get('status') in ('partial', 'unavailable'))):
                        for remaining in self.experiments[self.experiments.index(name) + 1:]:
                            self.record['coverage'].append(dict(experiment=remaining, status='not-run',
                                reasons=['Stopped after failed experiment ' + name]))
                        self.save()
                        return 1
                self.analyze(self.output / 'runs')
        except BaseException as error:
            self.terminal_error(error)
            raise
        finally:
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
    if args.list:
        print(json.dumps({'experiments': EXPERIMENTS, 'groups': GROUPS, 'automatic': {'figure-08-gpu': 'SSH GPU 0–7 admission; optional, reported in result.md'}}, indent=2))
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
    if args.resume and (args.output or args.analyze_existing):
        p.error('--resume cannot be combined with --output/--analyze-existing')
    config = {} if args.analyze_existing else load_config(args.config.resolve())
    check_timeout(config)
    output = (args.resume or args.output or default_output(args)).resolve()
    if args.analyze_existing and not args.analyze_existing.is_dir():
        p.error('--analyze-existing must point to an existing directory')
    runner = Review(args, config, output)
    output.mkdir(parents=True, exist_ok=bool(args.resume))
    if args.resume:
        history = output / 'attempt-history' / runner.attempt
        history.mkdir(parents=True, exist_ok=False)
        for name in ('review.json', 'SUMMARY.md', 'result.md'):
            if (output / name).exists():
                shutil.copy2(output / name, history / name)
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
    print(f'{runner.record["status"]}: {output / "SUMMARY.md"}', flush=True)
    return code


if __name__ == '__main__':
    install_termination_handler()
    raise SystemExit(main())
