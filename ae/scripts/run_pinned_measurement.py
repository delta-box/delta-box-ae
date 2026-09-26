#!/usr/bin/env python3
"""Run one command with scoped maximum-frequency policy and strict NUMA binding.

Requires root and explicit host-owner authorization. Only the selected CPUs'
policies change; all changes are recorded and restored on normal exit/signals.
The requested maximum P-state is not a guarantee of physical turbo frequency.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import time


def cpu_set(text):
    result = set()
    for item in text.replace(' ', ',').split(','):
        if not item:
            continue
        limits = item.split('-')
        if len(limits) == 1:
            result.add(int(item))
        elif len(limits) == 2 and int(limits[0]) <= int(limits[1]):
            result.update(range(int(limits[0]), int(limits[1])+1))
        else:
            raise ValueError('Invalid CPU range: '+item)
    return result


FIELDS = ('scaling_governor', 'scaling_min_freq', 'scaling_max_freq')


def state(policy):
    return {name: (policy/name).read_text().strip() for name in FIELDS}


def restore(policy, old):
    # Widen the allowed interval before restoring a possibly lower old maximum.
    (policy/'scaling_min_freq').write_text((policy/'cpuinfo_min_freq').read_text())
    (policy/'scaling_max_freq').write_text(old['scaling_max_freq'])
    (policy/'scaling_min_freq').write_text(old['scaling_min_freq'])
    (policy/'scaling_governor').write_text(old['scaling_governor'])
    if state(policy) != old:
        raise RuntimeError('Policy restore did not read back: '+str(policy))


def stop(process, grace=30):
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()



def acquire_node_lock(node, *, timeout, root=Path('/run/lock')):
    path = root / f'deltabox-numa-{node}.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    lock = os.fdopen(fd, 'a')
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise ValueError('Unsafe NUMA lease file')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Timed out waiting for NUMA node ' + str(node))
                time.sleep(0.2)
    except BaseException:
        lock.close()
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--node', type=int, required=True)
    p.add_argument('--cpus', required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--timeout', type=float, default=7200)
    p.add_argument('--stop-grace', type=float, default=30, help='Child cleanup grace, including RAM evidence archival')
    p.add_argument('command', nargs=argparse.REMAINDER)
    args = p.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if os.geteuid() != 0 or not command or args.timeout <= 0 or args.stop_grace <= 0:
        p.error('Root, a command after --, and a positive timeout are required')
    cpus = cpu_set(args.cpus)
    node = Path(f'/sys/devices/system/node/node{args.node}')
    if not cpus or not cpus <= cpu_set((node/'cpulist').read_text()) or not cpus <= os.sched_getaffinity(0):
        p.error('Selected CPUs must be available and entirely inside the requested node')
    if not all(shutil.which(name) for name in ('numactl', 'turbostat')):
        p.error('numactl and turbostat are required; effective frequency must be recorded')
    policies = sorted({(Path(f'/sys/devices/system/cpu/cpu{cpu}')/'cpufreq').resolve() for cpu in cpus})
    for policy in policies:
        if not cpu_set((policy/'related_cpus').read_text()) <= cpus:
            p.error('A shared frequency policy would change CPUs outside this selection')
        if 'performance' not in (policy/'scaling_available_governors').read_text().split():
            p.error('performance governor unavailable')
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'schema_version': 1, 'node': args.node, 'cpus': sorted(cpus),
                'command': ['numactl', '--physcpubind='+args.cpus, '--membind='+str(args.node), *command],
                'status': 'preparing', 'policies': {}, 'started_unix': time.time(),
                'frequency_note': 'min=max=cpuinfo_max_freq requests highest P-state; see turbostat Bzy_MHz for achieved frequency'}
    def save():
        temp = output/'environment.json.tmp'
        temp.write_text(json.dumps(manifest, indent=2)+'\n')
        temp.replace(output/'environment.json')
    locks, changed = [], []
    child = monitor = None
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, interrupted)
    save()
    try:
        manifest['status'] = 'waiting-for-numa'
        save()
        waiting = time.monotonic()
        locks.append(acquire_node_lock(args.node, timeout=args.timeout))
        manifest.update(status='preparing', resource_wait_s=time.monotonic() - waiting,
                        numa_lease=f'/run/lock/deltabox-numa-{args.node}.lock')
        save()
        for policy in policies:
            lock = open('/run/lock/deltabox-'+policy.name+'.lock', 'a')
            locks.append(lock)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            old = state(policy)
            maximum = (policy/'cpuinfo_max_freq').read_text().strip()
            manifest['policies'][str(policy)] = {'original': old, 'requested_khz': maximum}
            save()  # Recovery information is durable before the first write.
            changed.append((policy, old))
            (policy/'scaling_max_freq').write_text(maximum)
            (policy/'scaling_min_freq').write_text(maximum)
            (policy/'scaling_governor').write_text('performance')
            actual = state(policy)
            if actual != dict(scaling_governor='performance', scaling_min_freq=maximum, scaling_max_freq=maximum):
                raise RuntimeError('Cannot lock requested frequency policy: '+str(policy))
            manifest['policies'][str(policy)]['applied'] = actual
        manifest['intel_pstate'] = {}
        for name in ('status','no_turbo','min_perf_pct','max_perf_pct'):
            path = Path('/sys/devices/system/cpu/intel_pstate')/name
            if path.exists():
                manifest['intel_pstate'][name] = path.read_text().strip()
        with (output/'turbostat.stderr').open('w') as err, (output/'command.log').open('w') as log, (output/'samples.jsonl').open('w') as samples:
            monitor_command = ['turbostat','--quiet','--cpu',args.cpus,'--show','CPU,Busy%,Bzy_MHz,TSC_MHz,Avg_MHz',
                               '--interval','1','--out',str(output/'turbostat.tsv')]
            monitor = subprocess.Popen(monitor_command, stdout=err, stderr=err, start_new_session=True)
            time.sleep(1.2)
            if monitor.poll() is not None:
                raise RuntimeError('turbostat failed before workload; see turbostat.stderr')
            child = subprocess.Popen(manifest['command'], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            manifest.update(status='running', pid=child.pid, monitor_pid=monitor.pid, monitor_command=monitor_command)
            save()
            deadline = time.monotonic()+args.timeout
            while child.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Pinned measurement deadline exceeded')
                if monitor.poll() is not None:
                    raise RuntimeError('Frequency monitor exited during workload')
                sample = {'unix': time.time(), 'policies': {}}
                for policy, _ in changed:
                    measured = state(policy)
                    if measured != manifest['policies'][str(policy)]['applied']:
                        raise RuntimeError('Frequency policy changed during workload: '+str(policy))
                    sample['policies'][policy.name] = (policy/'scaling_cur_freq').read_text().strip()
                samples.write(json.dumps(sample)+'\n')
                samples.flush()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            manifest.update(returncode=child.returncode, status='ok' if child.returncode == 0 else 'failed')
    except BaseException as error:
        manifest.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, signal.SIG_IGN)
        errors = []
        for process in (child, monitor):
            try:
                stop(process, grace=args.stop_grace if process is child else 30)
            except Exception as error:
                errors.append(str(error))
        for policy, old in reversed(changed):
            try:
                restore(policy, old)
                manifest['policies'][str(policy)]['restored'] = state(policy)
            except Exception as error:
                errors.append(str(error))
        manifest.update(finished_unix=time.time(), restoration_errors=errors)
        if errors:
            manifest['status'] = 'failed'
        save()
        for lock in locks:
            lock.close()
        if errors:
            raise RuntimeError('Cleanup/restoration failed: '+repr(errors))
    return manifest.get('returncode', 1)


if __name__ == '__main__':
    raise SystemExit(main())
