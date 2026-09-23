#!/usr/bin/env python3
"""Owned single-Python dump/restore diagnostic, never a performance measurement."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--criu', type=Path, default=Path('/usr/sbin/criu'))
    parser.add_argument('--python', type=Path, default=Path('/usr/bin/python3.11'))
    parser.add_argument('--cpu', type=int, default=0)
    args = parser.parse_args()
    os.sched_setaffinity(0, {args.cpu})
    args.out.mkdir(parents=True, exist_ok=False)
    images = args.out / 'images'
    images.mkdir()
    log = (args.out / 'worker.log').open('w')
    code = ('import os,time,pathlib; marker=bytearray(b"owned-minimal-checkpoint"); '
            'pathlib.Path("ready").write_text(str(os.getpid())); time.sleep(120)')
    worker = subprocess.Popen([str(args.python), '-c', code], cwd=args.out,
                              stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                              start_new_session=True)
    result = {'purpose': 'non-performance minimal Python restore diagnosis',
              'criu': str(args.criu), 'python': str(args.python),
              'criu_sha256': hashlib.sha256(args.criu.read_bytes()).hexdigest(),
              'criu_version': subprocess.check_output([str(args.criu), '--version'], text=True).strip(),
              'kernel': os.uname().release, 'affinity': sorted(os.sched_getaffinity(0)),
              'original_pid': worker.pid}
    try:
        deadline = time.monotonic() + 5
        while not (args.out / 'ready').exists():
            if worker.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('minimal worker did not become ready')
            time.sleep(.01)
        for name in ('maps', 'status'):
            (args.out / ('worker.' + name)).write_text((Path('/proc') / str(worker.pid) / name).read_text())
        dump = [str(args.criu), 'dump', '--leave-running', '-t', str(worker.pid),
                '-D', str(images), '--shell-job', '--tcp-close', '--file-locks',
                '--ext-unix-sk', '--track-mem', '--log-file', 'dump-v4.log', '-v4']
        cp = subprocess.run(dump, capture_output=True, text=True, timeout=30)
        result['dump'] = {'command': dump, 'rc': cp.returncode, 'stdout': cp.stdout, 'stderr': cp.stderr}
        if cp.returncode:
            raise RuntimeError('minimal dump failed')
        worker.kill()
        worker.wait(timeout=5)
        restore = [str(args.criu), 'restore', '-d', '--leave-stopped', '-D', str(images),
                   '--shell-job', '--tcp-close', '--file-locks', '--ext-unix-sk',
                   '--pidfile', str(args.out / 'restored.pid'), '--log-file', 'restore-v4.log',
                   '--log-pid', '-v4']
        cp = subprocess.run(restore, capture_output=True, text=True, timeout=30)
        result['restore'] = {'command': restore, 'rc': cp.returncode, 'stdout': cp.stdout, 'stderr': cp.stderr}
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        pidfile = args.out / 'restored.pid'
        if pidfile.exists():
            pid = int(pidfile.read_text())
            if pid != worker.pid:
                raise RuntimeError('restored PID does not match this owned checkpoint')
            try:
                result['restored_state'] = (Path('/proc') / str(pid) / 'stat').read_text().rpartition(')')[2].split()[0]
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        log.close()
        (args.out / 'probe.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'out': str(args.out), 'dump_rc': result['dump']['rc'],
                      'restore_rc': result.get('restore', {}).get('rc'),
                      'restored_state': result.get('restored_state')}, indent=2))


if __name__ == '__main__':
    main()
