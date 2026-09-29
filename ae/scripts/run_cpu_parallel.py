"""Two bounded CPU lanes under one inherited, exclusive results lease."""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / 'ae')]
PLACEMENT = {1: '28-31', 2: '48-51'}
BASELINES = {'table-02-replay', 'table-02-criu', 'table-02-fc-diff',
             'table-02-cube', 'table-02-e2b', 'figure-08-cube', 'figure-08-e2b'}


def partition(experiments):
    return {node: [name for name in experiments if (name in BASELINES) == (node == 2)]
            for node in PLACEMENT}


def validate(args):
    if (args.group != ['cpu'] or args.experiment or args.all or args.quick_check
            or args.numa_node is not None or args.cpus or args.no_pin
            or args.gpu_cases or args.available or args.analyze_existing
            or args.isolated_validation or args.execute_plan or args.probe_plan
            or args.publish_output or args.reuse_completed_from
            or args.cube_profile or args.e2b_profile or args.experiment_config):
        raise ValueError('--cpu-parallel requires --group cpu with fixed NUMA1/2 placement; selection, placement, profile and internal overrides are not allowed')


def lane_arguments(args, node, output, experiments):
    command = ['--config', str(args.config.resolve()), '--numa-node', str(node),
               '--cpus', PLACEMENT[node], '--baseline-inputs', args.baseline_inputs]
    for name in experiments:
        command += ['--experiment', name]
    for key in ('limit', 'max_events'):
        if getattr(args, key) is not None:
            command += ['--' + key.replace('_', '-'), str(getattr(args, key))]
    lane = output / 'lanes' / ('numa' + str(node))
    command += ['--resume' if args.resume and (lane / 'review.json').is_file() else '--output', str(lane)]
    return command


def inherited_lease(fd, path):
    actual, expected = os.fstat(fd), path.stat()
    if (not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1
            or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)):
        raise ValueError('Worker did not inherit the results lease')
    # An inherited descriptor refers to the same open file description. This
    # retains the parent's exclusive flock instead of opening a second lease.
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def stop_owned(processes, grace=600):
    live = [p for p in processes if p.poll() is None]
    for process in live:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    forced = []
    for process in live:
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            forced.append(process.pid)
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return forced


def launch_lanes(commands, control, lease_fd, changed, *, poll_interval=0.5):
    """Wait for both owned processes; cancel the peer on failure or interruption."""
    processes, handles, rows = {}, [], {}
    try:
        for node, command in commands.items():
            directory = control / ('numa' + str(node))
            directory.mkdir(parents=True, exist_ok=False)
            log = (directory / 'stdout.log').open('wb')
            handles.append(log)
            process = subprocess.Popen(command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True, pass_fds=(lease_fd,))
            processes[node] = process
            rows[node] = dict(node=node, cpus=PLACEMENT[node], pid=process.pid,
                              status='running', log=str(directory / 'stdout.log'), command=command)
        changed(rows)
        while True:
            update = False
            for node, process in processes.items():
                code = process.poll()
                if code is not None and rows[node]['status'] == 'running':
                    rows[node].update(status='ok' if code == 0 else 'failed', returncode=code)
                    update = True
            if update:
                changed(rows)
            if any(row['status'] == 'failed' for row in rows.values()):
                forced = stop_owned(processes.values())
                for node, process in processes.items():
                    if rows[node]['status'] == 'running':
                        rows[node].update(status='cancelled', returncode=process.returncode,
                                          reason='Peer lane failed; owned cleanup requested')
                    if process.pid in forced:
                        rows[node]['cleanup_timeout'] = True
                changed(rows)
                return rows
            if all(row['status'] == 'ok' for row in rows.values()):
                return rows
            time.sleep(poll_interval)
    finally:
        forced = stop_owned(processes.values())
        for node, process in processes.items():
            if rows.get(node, {}).get('status') == 'running':
                rows[node].update(status='interrupted', returncode=process.returncode)
            if process.pid in forced:
                rows[node]['cleanup_timeout'] = True
        if rows:
            changed(rows)
        for handle in handles:
            handle.close()


def run(args, parser, config, lease_fd, review):
    validate(args)
    if review.validation_job_limit(config) is None:
        raise ValueError('Two-lane mode requires review.validation_max_jobs (1–10)')
    output = review.no_symlink_parents(args.resume or args.output or review.default_output(args)).resolve()
    root = REPO / 'ae/results'
    if output == root or not output.is_relative_to(root / 'selected') or output == root / 'selected':
        raise ValueError('Two-lane results must use a dedicated ae/results/selected/<run> directory')
    inherited_lease(lease_fd, REPO / 'ae/work/.results.lock')
    with review.output_tree_lock(REPO / 'ae/work', root, output):
        runner = review.Review(args, config, output)
        assignments = partition(review.CPU_EXPERIMENTS)
        policy = dict(mode='cpu-two-lane', lanes={str(n): {'node': n, 'cpus': PLACEMENT[n], 'experiments': exps}
                                                for n, exps in assignments.items()}, trace_workers_per_lane=1)
        if args.resume and runner.previous_record.get('concurrency_policy') != policy:
            raise ValueError('Resume requires the same two-lane layout')
        runner.record['concurrency_policy'] = policy
        output.mkdir(parents=True, exist_ok=bool(args.resume))
        if args.resume:
            history = output / 'attempt-history' / runner.attempt
            history.mkdir(parents=True, exist_ok=False)
            for name in ('review.json', 'result.md', 'SUMMARY.md'):
                if (output / name).is_file():
                    (history / name).write_bytes((output / name).read_bytes())
        runner.record['cpu_lanes'] = {}
        runner.save()
        print('Two CPU lanes: NUMA1 CPU28–31; NUMA2 CPU48–51. Output: ' + str(output), flush=True)
        commands = {node: ['numactl', '--all', '--physcpubind=' + PLACEMENT[node], '--membind=' + str(node),
                           sys.executable, '-I', str(Path(__file__).resolve()),
                           '--worker-node', str(node), '--lease-fd', str(lease_fd), '--',
                           *lane_arguments(args, node, output, exps)] for node, exps in assignments.items()}
        def changed(rows):
            runner.record['cpu_lanes'] = copy.deepcopy(rows)
            runner.save()
        failure = None
        rows = {}
        try:
            rows = launch_lanes(commands, output / 'control' / runner.attempt, lease_fd, changed)
        except BaseException as error:
            failure = error
            runner.record['error'] = f'{type(error).__name__}: {error}'
        finally:
            runner.record['coverage'] = []
            for node in assignments:
                path = output / 'lanes' / ('numa' + str(node)) / 'review.json'
                if path.is_file():
                    child = json.loads(path.read_text())
                    runner.record['coverage'].extend(child.get('coverage', []))
                    runner.record.setdefault('lane_reviews', {})[str(node)] = review.file_record(path)
            if any(row.get('successful_jobs', 0) for row in runner.record['coverage']):
                try:
                    runner.analyze(output / 'lanes')
                except BaseException as error:
                    failure = failure or error
                    runner.record['analysis_error'] = f'{type(error).__name__}: {error}'
            failed = failure is not None or len(rows) != 2 or any(row['status'] != 'ok' for row in rows.values())
            if not any(step['name'] == 'paper-comparison' and step['status'] == 'ok' for step in runner.record['steps']):
                failed = True
            runner.record.update(status='failed' if failed else 'ok', finished_at=datetime.now(timezone.utc).isoformat())
            runner.save()
            review.make_output_accessible(output)
        print(runner.record['status'] + ': ' + str(output / 'SUMMARY.md'), flush=True)
        if isinstance(failure, KeyboardInterrupt):
            return 130
        return 1 if failed else 0


def worker(argv=None):
    from ae.scripts import run_review as review
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-node', type=int, choices=PLACEMENT, required=True)
    parser.add_argument('--lease-fd', type=int, required=True)
    options, rest = parser.parse_known_args(argv)
    if rest[:1] == ['--']:
        rest = rest[1:]
    inherited_lease(options.lease_fd, REPO / 'ae/work/.results.lock')
    rp = review.parser()
    args = rp.parse_args(rest)
    expected = partition(review.CPU_EXPERIMENTS)[options.worker_node]
    if (args.experiment != expected or args.group or args.cpu_parallel or args.quick_check
            or args.all or args.no_pin or args.analyze_existing or args.available
            or args.numa_node != options.worker_node or args.cpus != PLACEMENT[options.worker_node]):
        raise ValueError('Invalid CPU lane selection or placement')
    args.cpu_parallel_lane = True
    config = review.load_config(args.config.resolve())
    review.check_timeout(config)
    review.apply_validation_defaults(args, config)
    output = review.no_symlink_parents(args.resume or args.output).resolve()
    root = REPO / 'ae/results/selected'
    if not output.is_relative_to(root) or output.name != 'numa' + str(options.worker_node) or output.parent.name != 'lanes':
        raise ValueError('Invalid CPU lane output')
    actual = sorted(os.sched_getaffinity(0))
    expected_cpus = [int(x) for part in args.cpus.split(',') for x in (range(int(part.split('-')[0]), int(part.split('-')[1])+1) if '-' in part else [part])]
    if actual != expected_cpus:
        raise ValueError('Worker CPU affinity differs from fixed lane')
    # Parent owns the exclusive result/output leases through all worker cleanup.
    return review.run_locked_selection(args, rp, config, output)


if __name__ == '__main__':
    from repro.common import install_termination_handler
    install_termination_handler()
    raise SystemExit(worker())
