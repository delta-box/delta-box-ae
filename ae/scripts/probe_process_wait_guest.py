#!/usr/bin/env python3
"""Probe pidfd compatibility in a disposable VM using a recorded run's images.

Run as root under unshare --mount --net --propagation private, outside timed runs.
The supplied run.json and its guest.tar must still exist at their recorded paths.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae/runners/deltabox'))
from run_instance import InstanceRun, managed_vm, upload_inputs, signature

PROBE = r'''
import ctypes, json, os, select, subprocess, sys, time
from unittest import mock
sys.path.insert(0, '/app')
import entry, process_wait
from pathlib import Path
entry.verify_sources(Path('/app'))
result = dict(python=sys.version, kernel=os.uname().release,
              python_pidfd_open=hasattr(os, 'pidfd_open'),
              libc_pidfd_open=hasattr(ctypes.CDLL(None), 'pidfd_open'))
opener = process_wait._pidfd_opener()
assert opener is not None, 'no pidfd backend'
fd = opener(os.getpid())
poll = select.poll(); poll.register(fd, select.POLLIN)
assert not poll.poll(0), 'running process unexpectedly ready'
os.close(fd)
before = len(os.listdir('/proc/self/fd'))
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.05)'])
try:
    # A constant False probe means only pidfd readiness can report completion.
    with mock.patch.object(process_wait.time, 'sleep', side_effect=AssertionError('used polling fallback')):
        unresolved = process_wait.wait_for_process_exit([child.pid], 2.0, lambda pid: False)
    assert not unresolved, unresolved
    assert child.wait(timeout=1) == 0
finally:
    if child.poll() is None: child.kill()
    child.wait()
assert len(os.listdir('/proc/self/fd')) == before, 'descriptor leak'
result.update(pidfd_readiness_without_probe=True, descriptor_leak=False, status='ok')
print('PROBE_JSON=' + json.dumps(result))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    config = json.loads(args.run_config.read_text())
    for key, recorded in config['images'].items():
        if signature(Path(config[key])) != {k: v for k, v in recorded.items() if k != 'sha256'}:
            raise RuntimeError('image changed since measurement: ' + key)
    config.update(experiment='pidfd-capability-probe', status='probe_pending',
                  source_run_config=str(args.run_config.resolve()), artifacts=[])
    spec = InstanceRun(args.out / 'run.json', config)
    spec.save()
    with managed_vm(spec) as machine:
        upload_inputs(machine, spec)
        result = subprocess.run(machine.ssh + ['python3 -'], input=PROBE,
                                text=True, capture_output=True, timeout=30)
        (args.out / 'probe.log').write_text(result.stdout + result.stderr)
        result.check_returncode()
        rows = [line.removeprefix('PROBE_JSON=') for line in result.stdout.splitlines()
                if line.startswith('PROBE_JSON=')]
        if len(rows) != 1:
            raise RuntimeError('missing probe output')
        (args.out / 'probe.json').write_text(rows[0] + '\n')
        spec.mark('probe_ok')
        print(rows[0])


if __name__ == '__main__':
    main()
